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

YTDL_BASE_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'restrictfilenames': True,
    'format': 'bestvideo*+bestaudio/best'
}

def is_valid_youtube_url(url):
    pattern = re.compile(r'^(https?://)?(www\.)?(m\.)?(youtube\.com|youtu\.be)/.+$')
    return bool(pattern.match(url))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/batch_download', methods=['POST'])
def batch_download():
    data = request.get_json(silent=True)
    urls = data.get('urls', [])
    cookies_path = data.get('cookies_path', None)

    if not urls or not isinstance(urls, list):
        return jsonify({'error': 'Provide a list of YouTube URLs'}), 400

    valid_urls = [u for u in urls if is_valid_youtube_url(u)]
    if not valid_urls:
        return jsonify({'error': 'No valid YouTube URLs found'}), 400

    results = []

    def download_video(url):
        with tempfile.TemporaryDirectory() as temp_dir:
            ydl_opts = {
                'format': 'bestvideo+bestaudio/best',
                'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
                'restrictfilenames': True,
                'merge_output_format': 'mp4'
            }

            if cookies_path:
                ydl_opts['cookiefile'] = cookies_path

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info)

                with open(filename, 'rb') as f:
                    buffer = BytesIO(f.read())
                    buffer.seek(0)

                clean_filename = os.path.basename(filename)
                video_id = str(uuid.uuid4())
                app.config.setdefault('videos', {})[video_id] = (buffer, clean_filename)

                return {'url': url, 'status': 'success', 'download_url': f'/download_file/{video_id}'}

            except Exception as e:
                return {'url': url, 'status': 'error', 'message': str(e)}

    futures = [executor.submit(download_video, u) for u in valid_urls]
    results = [f.result() for f in futures]

    return jsonify({'results': results})

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