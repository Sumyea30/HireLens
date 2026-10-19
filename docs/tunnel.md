# Cloudflare Tunnel (remote interview links)

Tunnel the **frontend** origin so `/join`, `/api`, and `/ws` stay on one public HTTPS host. Candidates open the emailed join link from anywhere.

## Quick try (ephemeral URL)

1. Start the API (`uvicorn` on `:8000`) and the Vite app (`npm run dev` on `:5173`).
2. Install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/).
3. Run:

```bash
cloudflared tunnel --url http://localhost:5173
```

4. Copy the printed `https://….trycloudflare.com` URL into backend env:

```env
PUBLIC_BASE_URL=https://YOUR-SUBDOMAIN.trycloudflare.com
CORS_ORIGINS=http://localhost:5173,https://YOUR-SUBDOMAIN.trycloudflare.com
TRANSCRIPT_DIR=stored_transcripts
```

5. Restart the API so invite emails and ICS use the public base URL.
6. Schedule an interview. The candidate link is `{PUBLIC_BASE_URL}/join/{token}`.

Vite already proxies `/api` and `/ws` to the API, so one tunnel is enough for REST and signalling.

## Named tunnel (stable hostname)

Use a Cloudflare Zero Trust named tunnel pointed at `http://localhost:5173` (or at nginx `:8080` if you use Docker Compose frontend). Set `PUBLIC_BASE_URL` / `CORS_ORIGINS` to that hostname the same way.

## Docker Compose

Tunnel `http://localhost:8080` (frontend nginx) instead of Vite. Keep `PUBLIC_BASE_URL` equal to the tunnel HTTPS origin. Set `TRANSCRIPT_DIR=/data/transcripts` on the API service (Compose mounts a volume for sealed JSON).

## WebRTC across networks

The tunnel carries the join page and WebSocket signalling only. Camera/mic media is peer-to-peer. Default ICE uses a public STUN server; many home/office NATs still need TURN.

Set optional ICE servers in `frontend/.env` (restart Vite):

```env
VITE_ICE_SERVERS=[{"urls":"stun:stun.l.google.com:19302"},{"urls":"turn:openrelay.metered.ca:80","username":"openrelayproject","credential":"openrelayproject"}]
```

Replace with your own TURN credentials for production. Document any third-party STUN/TURN you use.
