#!/bin/bash
# start-proxy.sh — Arranca el proxy local + Cloudflare Tunnel
# La URL del túnel se actualiza automáticamente en D1 y Cloudflare Pages
# Ejecuta: bash start-proxy.sh

set -e
cd "$(dirname "$0")"

WRANGLER_DIR="$(dirname "$0")/../reloadtrack-app"
PROJECT="reloadtrack-app"
DB="reloadtrack"
TUNNEL_LOG="/tmp/cloudflare-tunnel-$$.log"

echo "🔄 Matando instancias anteriores..."
pkill -f "proxy_local.py" 2>/dev/null || true
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1

echo "🚀 Arrancando proxy en :5001..."
python3 proxy_local.py &
PROXY_PID=$!
sleep 2

# Verificar que el proxy arrancó
if ! curl -s --max-time 3 http://localhost:5001/health > /dev/null; then
  echo "❌ El proxy no arrancó. Comprueba errores arriba."
  kill $PROXY_PID 2>/dev/null || true
  exit 1
fi
echo "✅ Proxy OK (PID $PROXY_PID)"

echo ""
echo "🌐 Arrancando Cloudflare Tunnel..."

# Lanzar el túnel en background y capturar la URL
cloudflared tunnel --url http://localhost:5001 2>&1 | tee "$TUNNEL_LOG" &
TUNNEL_PID=$!

# Esperar a que aparezca la URL (hasta 30 segundos)
TUNNEL_URL=""
echo "   ⏳ Esperando URL del túnel..."
for i in $(seq 1 30); do
  TUNNEL_URL=$(grep -oP 'https://[a-z0-9\-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
  if [ -n "$TUNNEL_URL" ]; then
    break
  fi
  sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
  echo "   ⚠️  No se detectó la URL del túnel. Actualízala manualmente."
  wait $TUNNEL_PID
  exit 0
fi

echo ""
echo "   ✅ Túnel activo: $TUNNEL_URL"
echo ""
echo "   🔄 Actualizando URL en Cloudflare Pages y D1..."

# Actualizar Pages secret
echo "$TUNNEL_URL" | npx --prefix "$WRANGLER_DIR" wrangler pages secret put YTDLP_PROXY_URL \
  --project-name "$PROJECT" 2>&1 | grep -E "✨|✘|Error" || true

# Actualizar D1
npx --prefix "$WRANGLER_DIR" wrangler d1 execute "$DB" --remote \
  --command "INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES ('proxy_tunnel_url', '$TUNNEL_URL', unixepoch())" \
  2>&1 | grep -E "✨|✘|Error|ok" || true

echo ""
echo "   🎉 Todo listo. URL activa: $TUNNEL_URL"
echo "   📱 Puedes enviar playlists de Spotify por Telegram ahora."
echo ""
echo "   (Pulsa Ctrl+C para detener el proxy y el túnel)"
echo ""

# Esperar a que el túnel termine
wait $TUNNEL_PID
