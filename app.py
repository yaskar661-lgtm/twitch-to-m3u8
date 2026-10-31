import time
import requests
import logging
import os
from flask import Flask, Response, request, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Loglama ayarları
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

# Rate Limiter Kurulumu (1 saniyede / dakikada aşırı istek gelmesini engellemek için)
# Render arkasında çalıştığı için get_remote_address fonksiyonu X-Forwarded-For başlığını dikkate alır.
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per minute"] # Genel bir sınır
)

# Bellek içi cache: { channel_name: {"urls": {...}, "expires_at": timestamp} }
STREAM_CACHE = {}
CACHE_TTL = 180  # 3 dakika

def fetch_twitch_master_data(channel: str, force_refresh: bool = False):
    now = time.time()
    channel_key = channel.lower()
    
    # Cache kontrolü (Eğer zorunlu yenileme istenmediyse ve cache geçerliyse)
    if not force_refresh and channel_key in STREAM_CACHE:
        item = STREAM_CACHE[channel_key]
        if item["expires_at"] > now:
            return item["urls"]
            
    # pwn.sh API'sinden akış adreslerini çek
    api_url = f"https://pwn.sh/tools/streamapi.py?url=https://twitch.tv/{channel_key}"
    try:
        resp = requests.get(api_url, timeout=10)
        data = resp.json()
        
        if data.get("success") is True or data.get("success") == "true":
            urls = data.get("urls", {})
            if urls:
                STREAM_CACHE[channel_key] = {
                    "urls": urls,
                    "expires_at": now + CACHE_TTL
                }
                logging.info(f"Başarılı: '{channel_key}' için taze linkler alındı ve önbelleklendi.")
                return urls
    except Exception as e:
        logging.error(f"API Hatası ({channel_key}): {e}")
        
    return None

def clean_twitch_manifest(m3u8_content: str) -> str:
    """
    Media playlist içindeki daterange reklamlarını ve Amazon reklam segmentlerini temizler.
    """
    lines = m3u8_content.splitlines()
    new_lines = []
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Reklam aralığı daterange etiketlerini atla
        if line.startswith("#EXT-X-DATERANGE"):
            i += 1
            continue
            
        # Segment başlığı ve URL eşleşmesi
        if "#EXTINF" in line:
            extinf_line = line
            segment_url_line = lines[i+1] if i + 1 < len(lines) else ""
            
            # Amazon (Twitch reklam sunucusu) geçen segmentleri atla
            if "Amazon" in extinf_line:
                i += 2
                continue
                
            new_lines.append(extinf_line)
            new_lines.append(segment_url_line)
            i += 2
            continue
            
        new_lines.append(line)
        i += 1
        
    return "\n".join(new_lines)


@app.route("/stream/<channel>.m3u8")
@limiter.limit("30 per minute") # Kanal başı/IP başı master istek sınırı
def master_playlist(channel):
    urls = fetch_twitch_master_data(channel)
    if not urls:
        return abort(404, description="Kanal çevrimdışı veya Twitch üzerinde bulunamadı.")
        
    master_lines = [
        "#EXTM3U",
        '#EXT-X-SESSION-DATA:DATA-ID="NODE",VALUE="cloudfront_proxy_node"',
        '#EXT-X-SESSION-DATA:DATA-ID="USER-COUNTRY",VALUE="TR"',
        '#EXT-X-SESSION-DATA:DATA-ID="SERVER-TIME",VALUE="%s"' % time.time()
    ]
    
    for quality, stream_url in urls.items():
        if "audio" in quality:
            master_lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH=160000,CODECS="mp4a.40.2",STABLE-VARIANT-ID="audio_only",IVS-NAME="{quality}"')
        else:
            master_lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1024x576,CODECS="avc1.4D401F,mp4a.40.2",STABLE-VARIANT-ID="{quality}",IVS-NAME="{quality}"')
            
        master_lines.append(f"/stream/media/{channel.lower()}/{quality}.m3u8")
        
    return Response("\n".join(master_lines), content_type="application/vnd.apple.mpegurl")


@app.route("/stream/media/<channel>/<quality>.m3u8")
@limiter.limit("120 per minute") # Video oynatıcılar segmentleri sık çekeceği için limiti biraz daha esnek tutuyoruz
def media_playlist(channel, quality):
    urls = fetch_twitch_master_data(channel)
    if not urls or quality not in urls:
        return abort(404, description="İstenen kalite veya kanal bulunamadı.")
        
    stream_url = urls[quality]
    
    try:
        resp = requests.get(stream_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if resp.status_code == 200:
            raw_content = resp.text
            cleaned_content = clean_twitch_manifest(raw_content)
            return Response(cleaned_content, content_type="application/vnd.apple.mpegurl")
    except Exception as e:
        logging.error(f"Media Playlist Çekme Hatası: {e}")
        
    return abort(500, description="Media playlist yüklenemedi.")


@app.route("/stream/refresh/<channel>")
@limiter.limit("5 per minute") # Manuel tazeleme suistimalini önlemek için sıkı tutuldu
def restart_channel(channel):
    channel_key = channel.lower()
    
    # 1. Önbellekten kanalı sil
    if channel_key in STREAM_CACHE:
        del STREAM_CACHE[channel_key]
    
    # 2. Hemen arkasından taze linkleri çekmeye zorla (Pre-fetch)
    fresh_urls = fetch_twitch_master_data(channel_key, force_refresh=True)
    
    if fresh_urls:
        return f"OK: '{channel}' yayını başarıyla tazelendi ve önbellek güncellendi.", 200
    else:
        return f"Hata: '{channel}' kanalı şu an çevrimdışı veya taze link alınamadı.", 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
