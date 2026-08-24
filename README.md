# S-localhost

A small, good-looking desktop app (Python + CustomTkinter) that turns any folder
on your computer into a live local website — with one click, and a QR code
you can scan to open it on your phone instantly (as long as your phone is on
the same Wi‑Fi network).

## What it does

- **Two source modes**, switchable at the top of the sidebar:
  - **Folder** — pick any folder and it becomes the root of a real local
    website (`index.html` inside it is served automatically).
  - **Single HTML** — pick *any* standalone `.html` file, anywhere, and the
    app serves it as if it were `index.html` — no renaming, no moving files.
    Other files sitting next to it (CSS, JS, images) are still reachable
    normally, since the whole folder is served alongside the alias.
- Choose a port (defaults to `8000`).
- Click **Start Server** — the app spins up a local server in the background
  and shows you the URL (e.g. `http://192.168.1.23:8000`).
- A **QR code** for that exact URL appears — scan it with your phone's camera
  to open the site immediately, no typing required.
- Copy the URL, open it in your own browser, and watch a live activity log
  of every request hitting the server.
- Click **Stop Server** to shut it down cleanly.

### Optional: self-signed HTTPS + HTTP → HTTPS redirect

Tick **Enable HTTPS (self-signed)** before starting the server to:

- Generate a fresh self-signed TLS certificate for `localhost` and your
  machine's current LAN IP (stored under `~/.s-localhost/certs/`), and
  serve the site over `https://` on your chosen port.
- Optionally run a second, plain HTTP server (**Redirect HTTP → HTTPS**,
  on its own port — default `8080`) whose only job is to 301-redirect any
  request straight to the HTTPS URL, so visitors typing `http://` still
  land on the secure site.

Because the certificate is self-signed (not issued by a public authority),
browsers and phones will show a "not secure" / trust warning the first time
you visit — this is expected. Tap **Advanced → Proceed** (wording varies by
browser) to continue. The app shows a reminder about this once HTTPS is on.

### Optional: a custom name instead of an IP address (mDNS / `.local`)

Tick **Use custom name (mDNS .local)** and type a name (e.g. `my-site`)
before starting the server to publish that name on the local network via
mDNS/Bonjour — the URL becomes `http://my-site.local:8000` (or `https://…`
if HTTPS is on too) instead of `http://192.168.1.23:8000`. The QR code
encodes this friendly URL, and the raw IP address is still shown underneath
as a fallback.

- Works out of the box on **macOS, iOS, and Linux with Avahi** (Bonjour /
  mDNS support is built in).
- On **Windows**, install "Bonjour Print Services" (or iTunes, which
  bundles it) for `.local` names to resolve.
- On **Android**, support is inconsistent between devices/browsers — if
  the `.local` name doesn't load, use the IP fallback shown under the URL
  (or scan the QR code again after switching the toggle off).
- Only devices on the **same Wi‑Fi/LAN** can resolve the name, same as the
  IP address itself.

## Setup

1. Make sure you have **Python 3.9+** installed.
2. (Linux only) Tkinter isn't always bundled with Python — install it first:
   ```bash
   sudo apt install python3-tk
   ```
3. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the app:
   ```bash
   python app.py
   ```

## Usage

1. Pick a source mode: **Folder** (a full website's files) or
   **Single HTML** (one standalone `.html` file).
2. Click **Choose Folder** / **Choose HTML File** and select it (a folder
   needs an `index.html` at the top level to load a homepage automatically —
   otherwise you'll get a file listing).
3. Set a port if you want something other than `8000`.
4. (Optional) Tick **Enable HTTPS (self-signed)** for `https://`, and
   optionally **Redirect HTTP → HTTPS** with its own port (default `8080`).
5. (Optional) Tick **Use custom name (mDNS .local)** and type a friendly
   name to get a URL like `my-site.local` instead of a raw IP address.
6. Click **▶ Start Server**.
7. Scan the QR code with your phone (same Wi‑Fi network as your computer),
   or click **Open in Browser** / **Copy URL** on your own machine. If
   HTTPS is on, accept the one-time self-signed certificate warning.
8. Click **■ Stop Server** whenever you're done.

## Notes

- The server binds to `0.0.0.0`, so it's reachable from any device on your
  local network — not just `localhost`. That's what makes the QR code work.
- If a port is already in use, the app will tell you in the activity log —
  just pick a different port and try again.
- Ports below 1024 (like the standard `80` for HTTP or `443` for HTTPS)
  usually require administrator/root privileges — stick to ports `1024+`
  (like the `8000`/`8080`/`8443` defaults) to avoid permission errors.
- This is meant for local development / sharing on a trusted network, not
  for exposing a site to the public internet.

## Links

The bottom of the sidebar has quick-access buttons for:

- **View Source Code** — https://github.com/sgdev-official/S-Localhost
- **License** — https://github.com/sgdev-official/S-Localhost
- **Android App (Mobile Viewer)** — https://github.com/sgdev-official/S-Localhost/releases/tag/1.2
