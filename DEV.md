# Dev & Phone Testing

## Phone URLs (while dev-server.py is running on the MacBook)

**Stable (use this one, bookmark to home screen)**
http://Evers-MacBook-Air.local:8000/

**Fallback if `.local` resolution fails** (IP changes with DHCP)
http://10.8.33.66:8000/

**State reset** — append `?reset` to either URL to nuke localStorage + Service Worker cache when the app gets weird after an update.
http://Evers-MacBook-Air.local:8000/?reset

---

## Requirements for phone to reach it
- Phone + MacBook on the same wifi
- `dev-server.py` running on the MacBook
- That's it. No tunnel, no deploy, no public URL.

---

## Start the server

```
cd ~/code/evers-menu
python3 dev-server.py
```

`dev-server.py` serves with aggressive no-cache + strips `If-Modified-Since` so iPhone Safari can't cling to stale HTML mid-iteration.

Vanilla `python3 -m http.server 8000` works too but Safari will 304-cache and you'll chase ghosts on reload.

---

## Home-screen PWA bookmark (iOS)

1. Safari → open the stable URL above
2. Share button → "Add to Home Screen"
3. Icon launches full-screen, no Safari chrome. The Service Worker gives offline mode.

Bookmark the `.local` URL, not the IP. The IP changes when wifi/DHCP does.

---

## GitHub mobile app

Fine for reading this file or viewing code. NOT a usable way to run the app — the GitHub app can't run PWAs. Use Safari.

---

## When the stable URL stops working

The `.local` hostname depends on mDNS/Bonjour. If something blocks it (some captive portals, guest wifi, VPN split-tunneling), fall back to the IP or run:

```
ipconfig getifaddr en0
```

on the MacBook to get the current IP, then use `http://<that-ip>:8000/`.

---

## Longer-term fix

For a URL that works off-wifi (e.g., at a friend's place, on cellular): Tailscale or Cloudflare Pages + Access. Deferred — kitchen test on LAN first.
