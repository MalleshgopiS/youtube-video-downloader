from flask import Flask, request, jsonify, send_file, abort, render_template
import subprocess
import tempfile
import uuid
import os
import re
import json
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=10)

def is_valid_youtube_url(url):
    pattern = re.compile(r'^(https?://)?(www\.)?(m\.)?(youtube\.com|youtu\.be)/.+$')
    return bool(pattern.match(url))

@app.route('/')
def index():
    return render_template('index.html')

def get_video_info_cli(url):
    try:
        result = subprocess.run(
            ['yt-dlp', '--dump-json', '--no-warnings', '-f', 'bestvideo*+bestaudio/best', url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"yt-dlp error: {e.stderr}")

@app.route('/formats', methods=['POST'])
def get_formats():
    data = request.get_json(silent=True)
    url = data.get('url', '').strip()

    if not is_valid_youtube_url(url):
        return jsonify({'error': 'Invalid or missing YouTube URL'}), 400

    try:
        info = get_video_info_cli(url)

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
        return jsonify({'error': f'Failed to retrieve formats: {str(e)}'}), 500

def download_video_cli(url, format_id, temp_dir):
    output_template = os.path.join(temp_dir, '%(title)s.%(ext)s')

    cmd = [
        'yt-dlp',
        '-f', f'{format_id}+bestaudio/best',
        '-o', output_template,
        '--merge-output-format', 'mp4',
        '--no-warnings',
        '--quiet',
        url
    ]

    try:
        subprocess.run(cmd, check=True)

        # Get the downloaded file (assumes only one file will be in the temp dir)
        for filename in os.listdir(temp_dir):
            if filename.endswith('.mp4'):
                return os.path.join(temp_dir, filename)

        raise FileNotFoundError("Downloaded file not found in temp dir.")

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"yt-dlp download failed: {e.stderr}")

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
            file_path = download_video_cli(url, format_id, temp_dir)

            with open(file_path, 'rb') as f:
                buffer = BytesIO(f.read())
                buffer.seek(0)

            clean_filename = os.path.basename(file_path)
            video_id = str(uuid.uuid4())
            return video_id, buffer, clean_filename

    try:
        future = executor.submit(download_task, url, format_id)
        video_id, buffer, clean_filename = future.result()

        app.config.setdefault('videos', {})[video_id] = (buffer, clean_filename)

        return jsonify({'download_url': f'/download_file/{video_id}'})

    except Exception as e:
        return jsonify({'error': f'Error downloading video: {str(e)}'}), 500

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
