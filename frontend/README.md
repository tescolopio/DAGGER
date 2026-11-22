# DAGGER frontend

Start the DAGGER backend on `127.0.0.1:8420`, then run:

```sh
cd frontend
npm install
npm run dev
```

The Vite development server listens on loopback and proxies `/api` and `/ws`
to the backend. `npm run build` type-checks and builds the production assets.
Serve those assets from the backend origin (or proxy the API and websocket
paths to it). The graph hydrates from run events and reconnects its websocket;
select a run in the graph header to inspect its nodes and event history.
Tasks move through the board by dragging or using the status menu. Review tasks
must be approved rather than moved directly to Done.
