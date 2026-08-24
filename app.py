"""
S-localhost
A professional local web server app with a CustomTkinter UI.

- Serve a whole folder, OR a single standalone .html file (loaded as index.html)
- Scan the generated QR code to open the site instantly on your phone
- Optional self-signed HTTPS, with an HTTP server that auto-redirects to HTTPS
"""

import os
import re
import ssl
import queue
import socket
import ipaddress
import threading
import webbrowser
import http.server
import socketserver
from datetime import datetime, timedelta, timezone
from tkinter import filedialog

import customtkinter as ctk
import qrcode
from zeroconf import Zeroconf, ServiceInfo
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

ACCENT = "#2fa572"
ACCENT_HOVER = "#26845c"
DANGER = "#c0392b"
DANGER_HOVER = "#992d22"

CERT_DIR = os.path.join(os.path.expanduser("~"), ".s-localhost", "certs")
CERT_PATH = os.path.join(CERT_DIR, "cert.pem")
KEY_PATH = os.path.join(CERT_DIR, "key.pem")


# --------------------------------------------------------------------------- #
# Self-signed certificate generation
# --------------------------------------------------------------------------- #

def generate_self_signed_cert(ip: str):
    """Generates (or refreshes) a self-signed cert/key pair valid for
    localhost + the machine's current LAN IP, and writes them to CERT_DIR."""
    os.makedirs(CERT_DIR, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "S-localhost Self-Signed"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "S-localhost"),
    ])

    san_entries = [x509.DNSName("localhost")]
    for candidate in (ip, "127.0.0.1"):
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(candidate)))
        except ValueError:
            pass

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(key, hashes.SHA256())
    )

    with open(KEY_PATH, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    with open(CERT_PATH, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    return CERT_PATH, KEY_PATH


# --------------------------------------------------------------------------- #
# Server internals
# --------------------------------------------------------------------------- #

class QueueLoggingHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that:
    - pushes log lines to a queue for the GUI activity log
    - optionally treats a specific file as '/' (single-file mode)
    """

    log_queue: "queue.Queue|None" = None
    index_alias: "str|None" = None

    def _apply_alias(self):
        if self.path == "/" and QueueLoggingHandler.index_alias:
            self.path = "/" + QueueLoggingHandler.index_alias

    def do_GET(self):
        self._apply_alias()
        super().do_GET()

    def do_HEAD(self):
        self._apply_alias()
        super().do_HEAD()

    def log_message(self, fmt, *args):
        message = f"{self.address_string()}  →  {fmt % args}"
        if QueueLoggingHandler.log_queue is not None:
            QueueLoggingHandler.log_queue.put(message)


class RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Answers every request with a 301 redirect to the HTTPS site."""

    https_port: "int|None" = None
    log_queue: "queue.Queue|None" = None

    def _redirect(self):
        host = self.headers.get("Host", "")
        hostname = host.split(":")[0] if host else self.client_address[0]
        location = f"https://{hostname}:{RedirectHandler.https_port}{self.path}"
        self.send_response(301)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        self._redirect()

    def do_HEAD(self):
        self._redirect()

    def log_message(self, fmt, *args):
        if RedirectHandler.log_queue is not None:
            RedirectHandler.log_queue.put(f"[http→https redirect] {self.address_string()} → {fmt % args}")


class ServerThread(threading.Thread):
    """Runs a threaded TCP HTTP(S) server serving `directory` on `port`."""

    def __init__(self, directory: str, port: int, log_queue: "queue.Queue",
                 index_alias: "str|None" = None, use_ssl: bool = False,
                 cert_path: "str|None" = None, key_path: "str|None" = None):
        super().__init__(daemon=True)
        self.directory = directory
        self.port = port
        self.log_queue = log_queue
        self.index_alias = index_alias
        self.use_ssl = use_ssl
        self.cert_path = cert_path
        self.key_path = key_path
        self.httpd = None
        self.error = None
        self._ready = threading.Event()

    def run(self):
        QueueLoggingHandler.log_queue = self.log_queue
        QueueLoggingHandler.index_alias = self.index_alias

        def handler(*args, **kwargs):
            return QueueLoggingHandler(*args, directory=self.directory, **kwargs)

        try:
            socketserver.ThreadingTCPServer.allow_reuse_address = True
            self.httpd = socketserver.ThreadingTCPServer(("0.0.0.0", self.port), handler)
            if self.use_ssl:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(self.cert_path, self.key_path)
                self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        except (OSError, ssl.SSLError) as exc:
            self.error = str(exc)
            self._ready.set()
            return

        self._ready.set()
        self.httpd.serve_forever()

    def wait_ready(self, timeout=2.0):
        self._ready.wait(timeout)

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()


class RedirectServerThread(threading.Thread):
    """Runs a plain HTTP server whose only job is redirecting to HTTPS."""

    def __init__(self, port: int, https_port: int, log_queue: "queue.Queue"):
        super().__init__(daemon=True)
        self.port = port
        self.https_port = https_port
        self.log_queue = log_queue
        self.httpd = None
        self.error = None
        self._ready = threading.Event()

    def run(self):
        RedirectHandler.https_port = self.https_port
        RedirectHandler.log_queue = self.log_queue
        try:
            socketserver.ThreadingTCPServer.allow_reuse_address = True
            self.httpd = socketserver.ThreadingTCPServer(("0.0.0.0", self.port), RedirectHandler)
        except OSError as exc:
            self.error = str(exc)
            self._ready.set()
            return
        self._ready.set()
        self.httpd.serve_forever()

    def wait_ready(self, timeout=2.0):
        self._ready.wait(timeout)

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()


def get_local_ip() -> str:
    """Best-effort discovery of the machine's LAN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def sanitize_hostname(raw: str, fallback: str = "s-localhost") -> str:
    """Turns arbitrary input into a safe mDNS label: lowercase letters,
    digits, and hyphens only, no leading/trailing hyphen."""
    label = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower())
    label = re.sub(r"-{2,}", "-", label).strip("-")
    return label or fallback


class ZeroconfPublisher:
    """Publishes a custom '<name>.local' hostname on the LAN via mDNS, so
    other devices can reach the server by name instead of by IP address."""

    def __init__(self):
        self.zc: "Zeroconf|None" = None
        self.info: "ServiceInfo|None" = None
        self.hostname = ""

    def start(self, hostname: str, ip: str, port: int, use_ssl: bool):
        service_type = "_https._tcp.local." if use_ssl else "_http._tcp.local."
        instance_name = f"{hostname}.{service_type}"
        server = f"{hostname}.local."

        self.zc = Zeroconf()
        self.info = ServiceInfo(
            service_type,
            instance_name,
            addresses=[socket.inet_aton(ip)],
            port=port,
            server=server,
            properties={"path": "/"},
        )
        self.zc.register_service(self.info)
        self.hostname = hostname

    def stop(self):
        if self.zc and self.info:
            try:
                self.zc.unregister_service(self.info)
            except Exception:
                pass
        if self.zc:
            self.zc.close()
        self.zc = None
        self.info = None
        self.hostname = ""


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("S-localhost")
        self.geometry("1000x720")
        self.minsize(900, 640)

        self.server_thread: "ServerThread|None" = None
        self.redirect_thread: "RedirectServerThread|None" = None
        self.zeroconf_publisher: "ZeroconfPublisher|None" = None
        self.log_queue: "queue.Queue" = queue.Queue()
        self.selected_dir = os.getcwd()
        self.selected_file = None
        self.current_url = ""
        self.mode = "Folder"

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()
        self.after(200, self._poll_log_queue)

    # ---- layout -----------------------------------------------------------

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, width=290, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(20, weight=1)

        ctk.CTkLabel(
            sidebar, text="S-localhost",
            font=ctk.CTkFont(size=26, weight="bold")
        ).grid(row=0, column=0, padx=24, pady=(28, 2), sticky="w")

        ctk.CTkLabel(
            sidebar, text="Serve any site. Instantly.",
            font=ctk.CTkFont(size=12), text_color="gray60"
        ).grid(row=1, column=0, padx=24, pady=(0, 20), sticky="w")

        # Mode selector
        ctk.CTkLabel(
            sidebar, text="SOURCE", font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray50"
        ).grid(row=2, column=0, padx=24, pady=(6, 4), sticky="w")

        self.mode_switch = ctk.CTkSegmentedButton(
            sidebar, values=["Folder", "Single HTML"], command=self.on_mode_change
        )
        self.mode_switch.set("Folder")
        self.mode_switch.grid(row=3, column=0, padx=24, pady=(0, 10), sticky="ew")

        self.folder_label = ctk.CTkLabel(
            sidebar, text=self._short_path(self.selected_dir),
            wraplength=240, justify="left", font=ctk.CTkFont(size=12)
        )
        self.folder_label.grid(row=4, column=0, padx=24, pady=(0, 8), sticky="w")

        self.browse_btn = ctk.CTkButton(
            sidebar, text="Choose Folder", command=self.choose_source
        )
        self.browse_btn.grid(row=5, column=0, padx=24, pady=(0, 20), sticky="ew")

        # Port
        ctk.CTkLabel(
            sidebar, text="PORT", font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray50"
        ).grid(row=6, column=0, padx=24, pady=(6, 4), sticky="w")

        self.port_entry = ctk.CTkEntry(sidebar, placeholder_text="8000")
        self.port_entry.insert(0, "8000")
        self.port_entry.grid(row=7, column=0, padx=24, pady=(0, 16), sticky="ew")

        # HTTPS toggle
        self.https_var = ctk.BooleanVar(value=False)
        self.https_check = ctk.CTkCheckBox(
            sidebar, text="Enable HTTPS (self-signed)",
            variable=self.https_var, command=self.on_https_toggle
        )
        self.https_check.grid(row=8, column=0, padx=24, pady=(0, 10), sticky="w")

        self.redirect_var = ctk.BooleanVar(value=True)
        self.redirect_check = ctk.CTkCheckBox(
            sidebar, text="Redirect HTTP → HTTPS",
            variable=self.redirect_var, state="disabled"
        )
        self.redirect_check.grid(row=9, column=0, padx=24, pady=(0, 8), sticky="w")

        self.redirect_port_label = ctk.CTkLabel(
            sidebar, text="HTTP REDIRECT PORT", font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray50"
        )
        self.redirect_port_label.grid(row=10, column=0, padx=24, pady=(4, 4), sticky="w")
        self.redirect_port_entry = ctk.CTkEntry(sidebar, placeholder_text="8080", state="disabled")
        self.redirect_port_entry.insert(0, "8080")
        self.redirect_port_entry.grid(row=11, column=0, padx=24, pady=(0, 18), sticky="ew")

        # mDNS / custom .local name
        self.mdns_var = ctk.BooleanVar(value=False)
        self.mdns_check = ctk.CTkCheckBox(
            sidebar, text="Use custom name (mDNS .local)",
            variable=self.mdns_var, command=self.on_mdns_toggle
        )
        self.mdns_check.grid(row=12, column=0, padx=24, pady=(0, 8), sticky="w")

        self.mdns_name_label = ctk.CTkLabel(
            sidebar, text="CUSTOM NAME", font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray50"
        )
        self.mdns_name_label.grid(row=13, column=0, padx=24, pady=(4, 4), sticky="w")

        self.mdns_name_entry = ctk.CTkEntry(sidebar, placeholder_text="s-localhost", state="disabled")
        self.mdns_name_entry.insert(0, "s-localhost")
        self.mdns_name_entry.grid(row=14, column=0, padx=24, pady=(0, 2), sticky="ew")

        self.mdns_preview = ctk.CTkLabel(
            sidebar, text="→ s-localhost.local", font=ctk.CTkFont(size=11),
            text_color="gray50"
        )
        self.mdns_preview.grid(row=15, column=0, padx=24, pady=(2, 18), sticky="w")
        self.mdns_name_entry.bind("<KeyRelease>", self._update_mdns_preview)

        self.start_btn = ctk.CTkButton(
            sidebar, text="▶  Start Server", fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=14, weight="bold"), height=40,
            command=self.toggle_server
        )
        self.start_btn.grid(row=16, column=0, padx=24, pady=(6, 14), sticky="ew")

        self.status_dot = ctk.CTkLabel(
            sidebar, text="●  Offline", text_color="gray50",
            font=ctk.CTkFont(size=13, weight="bold")
        )
        self.status_dot.grid(row=17, column=0, padx=24, pady=(0, 10), sticky="w")

        ctk.CTkLabel(
            sidebar, text="Built with CustomTkinter",
            font=ctk.CTkFont(size=10), text_color="gray40"
        ).grid(row=21, column=0, padx=24, pady=(20, 6), sticky="sw")

        links = ctk.CTkFrame(sidebar, fg_color="transparent")
        links.grid(row=22, column=0, padx=24, pady=(0, 20), sticky="sw")

        ctk.CTkButton(
            links, text="View Source Code", font=ctk.CTkFont(size=11, underline=True),
            fg_color="transparent", hover_color="gray20", text_color="gray70",
            anchor="w", height=22, command=lambda: webbrowser.open(
                "https://github.com/sgdev-official/S-Localhost"
            )
        ).pack(fill="x", pady=1)

        ctk.CTkButton(
            links, text="License", font=ctk.CTkFont(size=11, underline=True),
            fg_color="transparent", hover_color="gray20", text_color="gray70",
            anchor="w", height=22, command=lambda: webbrowser.open(
                "https://github.com/sgdev-official/S-Localhost"
            )
        ).pack(fill="x", pady=1)

        ctk.CTkButton(
            links, text="Android App (Mobile Viewer)", font=ctk.CTkFont(size=11, underline=True),
            fg_color="transparent", hover_color="gray20", text_color="gray70",
            anchor="w", height=22, command=lambda: webbrowser.open(
                "https://github.com/sgdev-official/S-Localhost/releases/tag/1.2"
            )
        ).pack(fill="x", pady=1)

    def _build_main(self):
        main = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        main.grid_columnconfigure(0, weight=2)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(1, weight=1)

        # URL card
        url_card = ctk.CTkFrame(main, corner_radius=14)
        url_card.grid(row=0, column=0, sticky="ew", pady=(0, 20), padx=(0, 10))
        url_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            url_card, text="LOCAL URL", font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray50"
        ).grid(row=0, column=0, padx=20, pady=(16, 0), sticky="w")

        self.url_var = ctk.StringVar(value="Server not running")
        ctk.CTkLabel(
            url_card, textvariable=self.url_var, font=ctk.CTkFont(size=20, weight="bold")
        ).grid(row=1, column=0, padx=20, pady=(2, 4), sticky="w")

        self.fallback_url_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            url_card, textvariable=self.fallback_url_var, font=ctk.CTkFont(size=12),
            text_color="gray55"
        ).grid(row=2, column=0, padx=20, pady=(0, 2), sticky="w")

        self.https_note = ctk.CTkLabel(
            url_card, text="", font=ctk.CTkFont(size=11), text_color="#e0a030",
            wraplength=420, justify="left"
        )
        self.https_note.grid(row=3, column=0, padx=20, pady=(0, 6), sticky="w")

        btn_row = ctk.CTkFrame(url_card, fg_color="transparent")
        btn_row.grid(row=4, column=0, padx=20, pady=(0, 16), sticky="w")

        self.copy_btn = ctk.CTkButton(
            btn_row, text="Copy URL", width=110, state="disabled", command=self.copy_url
        )
        self.copy_btn.pack(side="left", padx=(0, 10))

        self.open_btn = ctk.CTkButton(
            btn_row, text="Open in Browser", width=150, state="disabled", command=self.open_url
        )
        self.open_btn.pack(side="left")

        # QR card
        qr_card = ctk.CTkFrame(main, corner_radius=14)
        qr_card.grid(row=0, column=1, rowspan=2, sticky="nsew", pady=(0, 20))

        ctk.CTkLabel(
            qr_card, text="SCAN TO OPEN", font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray50"
        ).pack(padx=20, pady=(18, 10))

        self.qr_label = ctk.CTkLabel(
            qr_card, text="Start the server\nto generate a QR code",
            text_color="gray50", justify="center", font=ctk.CTkFont(size=12)
        )
        self.qr_label.pack(padx=24, pady=10, expand=True)

        # Log card
        log_card = ctk.CTkFrame(main, corner_radius=14)
        log_card.grid(row=1, column=0, sticky="nsew")
        log_card.grid_rowconfigure(1, weight=1)
        log_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_card, text="ACTIVITY LOG", font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray50"
        ).grid(row=0, column=0, padx=20, pady=(16, 6), sticky="w")

        self.log_box = ctk.CTkTextbox(log_card, font=ctk.CTkFont(size=11, family="Consolas"))
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 20))
        self._log("Ready. Choose a folder or a single HTML file, then start the server.")
        self.log_box.configure(state="disabled")

    # ---- helpers ------------------------------------------------------------

    def _short_path(self, path: str) -> str:
        return path if len(path) <= 38 else "..." + path[-35:]

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _poll_log_queue(self):
        while not self.log_queue.empty():
            self._log(self.log_queue.get_nowait())
        self.after(200, self._poll_log_queue)

    # ---- mode / source selection --------------------------------------------

    def on_mode_change(self, value):
        self.mode = value
        if value == "Folder":
            self.browse_btn.configure(text="Choose Folder")
            self.folder_label.configure(text=self._short_path(self.selected_dir))
        else:
            self.browse_btn.configure(text="Choose HTML File")
            label = self._short_path(self.selected_file) if self.selected_file else "No file selected yet"
            self.folder_label.configure(text=label)

    def choose_source(self):
        if self.mode == "Folder":
            folder = filedialog.askdirectory(initialdir=self.selected_dir)
            if folder:
                self.selected_dir = folder
                self.folder_label.configure(text=self._short_path(folder))
                self._log(f"Folder set to: {folder}")
        else:
            file_path = filedialog.askopenfilename(
                title="Choose an HTML file",
                filetypes=[("HTML files", "*.html *.htm"), ("All files", "*.*")]
            )
            if file_path:
                self.selected_file = file_path
                self.folder_label.configure(text=self._short_path(file_path))
                self._log(f"HTML file set to: {file_path} (will be served as index.html)")

    def on_https_toggle(self):
        enabled = self.https_var.get()
        state = "normal" if enabled else "disabled"
        self.redirect_check.configure(state=state)
        self.redirect_port_entry.configure(state=state)

    def on_mdns_toggle(self):
        state = "normal" if self.mdns_var.get() else "disabled"
        self.mdns_name_entry.configure(state=state)

    def _update_mdns_preview(self, _event=None):
        clean = sanitize_hostname(self.mdns_name_entry.get())
        self.mdns_preview.configure(text=f"→ {clean}.local")

    # ---- server control -------------------------------------------------------

    def toggle_server(self):
        if self.server_thread is None:
            self.start_server()
        else:
            self.stop_server()

    def start_server(self):
        # Resolve source
        if self.mode == "Folder":
            directory = self.selected_dir
            index_alias = None
            if not os.path.isdir(directory):
                self._log("ERROR: Selected folder does not exist.")
                return
        else:
            if not self.selected_file or not os.path.isfile(self.selected_file):
                self._log("ERROR: Choose a valid HTML file first.")
                return
            directory = os.path.dirname(self.selected_file)
            index_alias = os.path.basename(self.selected_file)

        try:
            port = int(self.port_entry.get().strip() or "8000")
        except ValueError:
            self._log("ERROR: Port must be a number.")
            return

        use_ssl = self.https_var.get()
        cert_path = key_path = None
        ip = get_local_ip()

        if use_ssl:
            try:
                cert_path, key_path = generate_self_signed_cert(ip)
                self._log("Generated self-signed certificate for this session.")
            except Exception as exc:
                self._log(f"ERROR generating certificate: {exc}")
                return

        thread = ServerThread(
            directory, port, self.log_queue,
            index_alias=index_alias, use_ssl=use_ssl,
            cert_path=cert_path, key_path=key_path
        )
        thread.start()
        thread.wait_ready()

        if thread.error:
            self._log(f"ERROR starting server: {thread.error}")
            return

        self.server_thread = thread
        scheme = "https" if use_ssl else "http"
        ip_url = f"{scheme}://{ip}:{port}"

        # Optional mDNS custom hostname (e.g. http://my-site.local:8000)
        use_mdns = self.mdns_var.get()
        if use_mdns:
            hostname = sanitize_hostname(self.mdns_name_entry.get())
            self.mdns_name_entry.delete(0, "end")
            self.mdns_name_entry.insert(0, hostname)
            self._update_mdns_preview()

            publisher = ZeroconfPublisher()
            try:
                publisher.start(hostname, ip, port, use_ssl)
                self.zeroconf_publisher = publisher
                url = f"{scheme}://{hostname}.local:{port}"
                self.fallback_url_var.set(f"or by IP: {ip_url}")
                self._log(f"Published mDNS name: {hostname}.local → {ip} "
                          f"(devices with Bonjour/Avahi support can use this)")
            except Exception as exc:
                self._log(f"WARNING: mDNS publish failed ({exc}); using IP address instead.")
                url = ip_url
                self.fallback_url_var.set("")
        else:
            url = ip_url
            self.fallback_url_var.set("")

        self.current_url = url
        self.url_var.set(url)

        if use_ssl:
            self.https_note.configure(
                text="Self-signed certificate: your browser and phone will show a "
                     "'not secure' / trust warning the first time — tap Advanced → "
                     "Proceed to continue. This is expected for local certificates."
            )
        else:
            self.https_note.configure(text="")

        # Optional HTTP → HTTPS redirect server
        if use_ssl and self.redirect_var.get():
            try:
                redirect_port = int(self.redirect_port_entry.get().strip() or "8080")
            except ValueError:
                self._log("ERROR: HTTP redirect port must be a number. Skipping redirect server.")
                redirect_port = None

            if redirect_port is not None:
                rthread = RedirectServerThread(redirect_port, port, self.log_queue)
                rthread.start()
                rthread.wait_ready()
                if rthread.error:
                    self._log(f"ERROR starting redirect server: {rthread.error}")
                else:
                    self.redirect_thread = rthread
                    self._log(
                        f"HTTP → HTTPS redirect active: http://{ip}:{redirect_port} → {url}"
                    )

        self.copy_btn.configure(state="normal")
        self.open_btn.configure(state="normal")
        self.status_dot.configure(text="●  Live", text_color=ACCENT)
        self.start_btn.configure(text="■  Stop Server", fg_color=DANGER, hover_color=DANGER_HOVER)
        self._log(f"Server started at {url} — serving {directory}" +
                   (f" (index: {index_alias})" if index_alias else ""))
        self._generate_qr(url)

        # lock source/mode controls while running
        self.mode_switch.configure(state="disabled")
        self.browse_btn.configure(state="disabled")
        self.port_entry.configure(state="disabled")
        self.https_check.configure(state="disabled")
        self.redirect_check.configure(state="disabled")
        self.redirect_port_entry.configure(state="disabled")
        self.mdns_check.configure(state="disabled")
        self.mdns_name_entry.configure(state="disabled")

    def stop_server(self):
        if self.server_thread:
            self.server_thread.stop()
            self.server_thread = None
        if self.redirect_thread:
            self.redirect_thread.stop()
            self.redirect_thread = None
        if self.zeroconf_publisher:
            self.zeroconf_publisher.stop()
            self.zeroconf_publisher = None
            self._log("mDNS name unpublished.")

        self.url_var.set("Server not running")
        self.fallback_url_var.set("")
        self.https_note.configure(text="")
        self.copy_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.status_dot.configure(text="●  Offline", text_color="gray50")
        self.start_btn.configure(text="▶  Start Server", fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.qr_label.configure(image=None, text="Start the server\nto generate a QR code")
        self._log("Server stopped.")

        self.mode_switch.configure(state="normal")
        self.browse_btn.configure(state="normal")
        self.port_entry.configure(state="normal")
        self.https_check.configure(state="normal")
        if self.https_var.get():
            self.redirect_check.configure(state="normal")
            self.redirect_port_entry.configure(state="normal")
        self.mdns_check.configure(state="normal")
        if self.mdns_var.get():
            self.mdns_name_entry.configure(state="normal")

    def _generate_qr(self, url: str):
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(210, 210))
        self.qr_label.configure(image=ctk_img, text="")
        self.qr_label.image = ctk_img  # keep a reference

    def copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self.current_url)
        self._log("URL copied to clipboard.")

    def open_url(self):
        if self.current_url:
            webbrowser.open(self.current_url)

    def on_close(self):
        if self.server_thread:
            self.server_thread.stop()
        if self.redirect_thread:
            self.redirect_thread.stop()
        if self.zeroconf_publisher:
            self.zeroconf_publisher.stop()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
