from flask import Flask, request, jsonify, send_file, abort, render_template
import yt_dlp
import tempfile
import uuid
import os
from io import BytesIO
import re
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=10)

YTDL_OPTS_BASE = {
    'quiet': True,
    'no_warnings': True,
    'skip_download': True,
    'restrictfilenames': True,
    'format': 'bestvideo*+bestaudio/best',
    'cookiesfrombrowser': ('chrome',) , # Automatically extract cookies from Chrome
    'cookies': 'cookies.txt' 
}

def is_valid_youtube_url(url):
    pattern = re.compile(r'^(https?://)?(www\.)?(m\.)?(youtube\.com|youtu\.be)/.+$')
    return bool(pattern.match(url))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/formats', methods=['POST'])
def get_formats():
    data = request.get_json(silent=True)
    url = data.get('url', '').strip()

    if not is_valid_youtube_url(url):
        return jsonify({'error': 'Invalid or missing YouTube URL'}), 400

    try:
        ydl_opts = YTDL_OPTS_BASE.copy()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        seen_labels = set()

        for f in info.get('formats', []):
            if not f.get('format_id'):
                continue

            ext = f.get('ext', 'mp4')
            if ext == 'webm':
                continue

            vcodec = f.get('vcodec', 'none')
            acodec = f.get('acodec', 'none')

            size_bytes = f.get('filesize') or f.get('filesize_approx')
            if not size_bytes:
                continue

            size_mb = f"{size_bytes / (1024 * 1024):.1f} MB"

            if vcodec != 'none' and acodec != 'none':
                label = f"{f.get('height', 'unknown')}p (video + audio) - {size_mb}"
            elif vcodec != 'none':
                label = f"{f.get('height', 'unknown')}p - {size_mb}"
            elif acodec != 'none':
                label = f"{f.get('abr', 'unknown')}kbps (audio only) - {size_mb}"
            else:
                continue

            label_key = (label, ext)
            if label_key in seen_labels:
                continue
            seen_labels.add(label_key)

            formats.append({
                'format_id': f['format_id'],
                'ext': ext,
                'label': label,
            })

        formats.sort(key=lambda f: int(re.findall(r'\d+', f['label'])[0]) if re.findall(r'\d+', f['label']) else 0, reverse=True)

        return jsonify({'formats': formats})

    except Exception as e:
        error_message = str(e)
        if "Sign in to confirm you're not a bot" in error_message:
            return jsonify({
                'error': 'This video requires sign-in. Ensure you are logged into Chrome.'
            }), 403
        return jsonify({'error': f'Failed to retrieve formats: {error_message}'}), 500

@app.route('/download', methods=['POST'])
def download_video():
    data = request.get_json(silent=True)
    url = data.get('url', '').strip()
    format_id = data.get('format_id', '').strip()

    if not is_valid_youtube_url(url):
        return jsonify({'error': 'Invalid or missing YouTube URL'}), 400
    if not format_id:
        return jsonify({'error': 'Missing format_id'}), 400

    def download_task(url, format_id):
        with tempfile.TemporaryDirectory() as temp_dir:
            ydl_opts = {
                'format': f'{format_id}+bestaudio/best',
                'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
                'restrictfilenames': True,
                'merge_output_format': 'mp4',
                'cookiesfrombrowser': ('chrome',)  # Automatically use Chrome cookies
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)

            with open(filename, 'rb') as f:
                buffer = BytesIO(f.read())
                buffer.seek(0)

            clean_filename = os.path.basename(filename)
            video_id = str(uuid.uuid4())
            return video_id, buffer, clean_filename

    try:
        future = executor.submit(download_task, url, format_id)
        video_id, buffer, clean_filename = future.result()

        app.config.setdefault('videos', {})[video_id] = (buffer, clean_filename)

        return jsonify({'download_url': f'/download_file/{video_id}'})

    except Exception as e:
        error_message = str(e)
        if "Sign in to confirm you're not a bot" in error_message:
            return jsonify({
                'error': 'This video requires login. Please make sure you are logged into YouTube in Chrome.'
            }), 403
        return jsonify({'error': f'Error downloading video: {error_message}'}), 500

@app.route('/download_file/<video_id>')
def download_file(video_id):
    videos = app.config.get('videos', {})
    data = videos.pop(video_id, None)
    if not data:
        abort(404)
    buffer, filename = data
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='video/mp4')

if __name__ == '__main__':
    app.run(debug=True, threaded=True)
