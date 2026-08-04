#!/bin/bash
# start-proxy.sh — Arranca el proxy local + Cloudflare Tunnel
# La URL del túnel se actualiza automáticamente en D1 y Cloudflare Pages
# Ejecuta: bash start-proxy.sh

set -e
cd "$(dirname "$0")"

echo "📦 Instalando dependencias del sistema y de Python (esto puede tardar unos segundos la primera vez)..."

# 1. Herramientas del sistema necesarias para compilar librerías (como Pillow para las carátulas)
if ! brew ls --versions libjpeg zlib &> /dev/null; then
    echo "⚙️ Instalando librerías de sistema para imágenes (libjpeg, zlib)..."
    brew install libjpeg zlib || true
fi

# Exportar las rutas de Homebrew por si Python necesita compilar Pillow desde el código fuente
export CFLAGS="-I$(brew --prefix)/include"
export LDFLAGS="-L$(brew --prefix)/lib"
export CPATH="$(brew --prefix)/include"

if ! command -v ffmpeg &> /dev/null; then
    echo "⚙️ Instalando FFMPEG para el procesado de audio..."
    brew install ffmpeg || true
fi

if ! command -v cloudflared &> /dev/null; then
    echo "⚙️ Instalando Cloudflared (Túnel)..."
    brew install cloudflare/cloudflare/cloudflared || true
fi

# 2. Actualizar las herramientas base de Python
pip3 install --upgrade pip setuptools wheel --break-system-packages 2>/dev/null || pip3 install --upgrade pip setuptools wheel

# 3. Instalar Streamrip saltándose la compilación de Pillow 9
echo "📦 Instalando Streamrip y parcheando Pillow..."
pip3 install appdirs cleo tqdm requests pycryptodomex simple-term-menu aiohttp tomlkit aiodns aiofiles deezer-py m3u8 click mutagen pathvalidate Pillow --break-system-packages 2>/dev/null || pip3 install appdirs cleo tqdm requests pycryptodomex simple-term-menu aiohttp tomlkit aiodns aiofiles deezer-py m3u8 click mutagen pathvalidate Pillow 2>/dev/null || true
pip3 install streamrip --no-deps --break-system-packages 2>/dev/null || pip3 install streamrip --no-deps 2>/dev/null || true

# 4. Instalar los requisitos del proyecto (eliminamos streamrip de requirements para que no lo pise)
sed -i.bak '/streamrip/d' requirements.txt
pip3 install -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -r requirements.txt
playwright install 2>/dev/null || true



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
  TUNNEL_URL=$(grep -Eo 'https://[a-z0-9\-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
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
