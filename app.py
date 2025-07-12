from flask import Flask, request, jsonify, send_file, abort, render_template
import yt_dlp
import tempfile
import uuid
import os
from io import BytesIO # Re-import BytesIO
import re
from concurrent.futures import ThreadPoolExecutor
from yt_dlp.utils import DownloadError # Import DownloadError for specific handling

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=10)

# Configure app logger for better debugging
import logging
app.logger.setLevel(logging.INFO) # Set logging level to INFO or DEBUG for more verbosity

# Store downloaded video buffers and filenames in memory
# Structure: {video_id: (BytesIO_buffer, filename)}
app.config['videos'] = {}

YTDL_OPTS_BASE = {
    'quiet': True,
    'no_warnings': True,
    'skip_download': True,
    'restrictfilenames': True,
    'format': 'bestvideo*+bestaudio/best',
    'noplaylist': True, # Ensure only single video formats are fetched
    'extractor_retries': 3, # Add retries for robustness
    'retries': 10,
}

def is_valid_youtube_url(url):
    # More robust regex for YouTube URLs
    pattern = re.compile(
        r'^(https?://)?(www\.)?(m\.)?(youtube\.com|youtu\.be)/'
        r'(watch\?v=|embed/|v/|shorts/|ytscreeningroom\?v=|yt\.be/|user/\S+/|channel/\S+/|playlist\?list=|\S+)'
        r'([a-zA-Z0-9_-]{11})([&?#].*)?$'
    )
    return bool(pattern.match(url))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/formats', methods=['POST'])
def get_formats():
    data = request.get_json(silent=True)
    url = data.get('url', '').strip()

    app.logger.info(f"Received /formats request for URL: {url}")
    if not is_valid_youtube_url(url):
        app.logger.warning(f"Invalid URL: {url}")
        return jsonify({'error': 'Invalid or missing YouTube URL. Please check the format.'}), 400

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
            if ext == 'webm': # Skip webm as per previous logic
                continue

            vcodec = f.get('vcodec', 'none')
            acodec = f.get('acodec', 'none')

            is_video_only = vcodec != 'none' and acodec == 'none'
            is_audio_only = vcodec == 'none' and acodec != 'none'
            is_video_audio_combined = vcodec != 'none' and acodec != 'none'

            if not (is_video_only or is_audio_only or is_video_audio_combined):
                continue

            size_bytes = f.get('filesize') or f.get('filesize_approx')
            if not size_bytes:
                continue

            size_mb = f"{size_bytes / (1024 * 1024):.1f} MB"

            label = None
            if is_video_audio_combined:
                label = f"{f.get('height', 'unknown')}p (video + audio) - {size_mb}"
            elif is_video_only:
                label = f"{f.get('height', 'unknown')}p  - {size_mb}"
            elif acodec != 'none': # Check acodec explicitly for audio-only
                abr_label = f"{f['abr']}kbps" if f.get('abr') and f['abr'] != 'none' else 'audio'
                label = f"{abr_label} (audio only) - {size_mb}"
            else:
                continue

            label_key = (label, ext, f['format_id']) # Include format_id for uniqueness
            if label_key in seen_labels:
                continue
            seen_labels.add(label_key)

            formats.append({
                'format_id': f['format_id'],
                'ext': ext,
                'label': label,
            })

        # Sort formats: highest resolution first, then audio quality
        formats.sort(key=lambda f: (
            int(re.findall(r'\d+', f['label'])[0]) if 'video' in f['label'] and re.findall(r'\d+', f['label']) else 0,
            int(re.findall(r'\d+', f['label'])[0]) if 'audio' in f['label'] and re.findall(r'\d+', f['label']) else 0,
            'audio' in f['label'] # Puts audio at the end of resolution-based sort
        ), reverse=True)

        app.logger.info(f"Found {len(formats)} formats for {url}")
        return jsonify({'formats': formats})

    except DownloadError as e:
        error_message = f"YouTube DL Error: {e.args[0]}"
        app.logger.error(f"DownloadError in /formats: {error_message}")
        if "video unavailable" in error_message.lower():
            return jsonify({'error': 'The video is unavailable or private.'}), 404
        elif "age-restricted" in error_message.lower():
            return jsonify({'error': 'This video is age-restricted and cannot be downloaded.'}), 403
        elif "No such file or directory" in error_message or "Unable to extract" in error_message:
            return jsonify({'error': 'Could not process the video. It might be invalid or restricted.'}), 400
        else:
            return jsonify({'error': f'Failed to retrieve formats: {error_message}'}), 500
    except Exception as e:
        app.logger.critical(f"An unexpected error occurred in /formats: {str(e)}", exc_info=True)
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500

@app.route('/download', methods=['POST'])
def download_video():
    data = request.get_json(silent=True)
    url = data.get('url', '').strip()
    format_id = data.get('format_id', '').strip()

    app.logger.info(f"Received /download request for URL: {url}, Format ID: {format_id}")
    if not is_valid_youtube_url(url):
        app.logger.warning(f"Invalid URL for download: {url}")
        return jsonify({'error': 'Invalid or missing YouTube URL. Please check the format.'}), 400
    if not format_id:
        app.logger.warning("Missing format_id for download.")
        return jsonify({'error': 'No format selected. Please choose a format.'}), 400

    def download_task(url, format_id):
        # Use TemporaryDirectory to ensure cleanup of disk files
        with tempfile.TemporaryDirectory() as temp_dir:
            # Determine the correct format string based on if it's a combined/video-only or audio-only
            ydl_format_string = format_id
            merge_output = 'mp4' # Default merge to mp4

            # Heuristic to detect audio-only formats by common audio format IDs or 'audio' in ID
            if 'audio' in format_id.lower() or format_id in ['140', '251', '171', '250', '249', '139', '256', '258', '600']:
                ydl_format_string = format_id
                merge_output = None # Don't try to merge audio with video if it's audio only

            ydl_opts = {
                'format': ydl_format_string,
                'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
                'restrictfilenames': True,
                'noplaylist': True,
                'writedescription': False, # Reduce unnecessary files
                'writeinfojson': False,
                'writethumbnail': False,
                'keepvideo': False, # Ensure intermediate files are removed by yt-dlp itself
                'progress': False, # Disable progress bars in logs
            }
            if merge_output:
                ydl_opts['merge_output_format'] = merge_output

            app.logger.info(f"Starting yt-dlp download to temp dir: {temp_dir}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info) # This is the path to the downloaded file on disk

            app.logger.info(f"Downloaded file to disk: {filename}. Reading into BytesIO.")
            # Read the entire file into BytesIO buffer
            with open(filename, 'rb') as f:
                buffer = BytesIO(f.read())
            buffer.seek(0) # Rewind the buffer to the beginning

            clean_filename = os.path.basename(filename)
            video_id = str(uuid.uuid4())
            app.logger.info(f"File {clean_filename} buffered into memory with ID: {video_id}")
            return video_id, buffer, clean_filename

    try:
        future = executor.submit(download_task, url, format_id)
        video_id, buffer, clean_filename = future.result(timeout=900) # Increased timeout to 15 minutes

        app.config['videos'][video_id] = (buffer, clean_filename)
        app.logger.info(f"Download task completed for {video_id}. Stored in app.config['videos'].")

        return jsonify({'download_url': f'/download_file/{video_id}'})

    except DownloadError as e:
        error_message = f"YouTube DL Error: {e.args[0]}"
        app.logger.error(f"DownloadError in /download: {error_message}")
        if "video unavailable" in error_message.lower():
            return jsonify({'error': 'The video is unavailable or private for download.'}), 404
        elif "age-restricted" in error_message.lower():
            return jsonify({'error': 'This video is age-restricted and cannot be downloaded.'}), 403
        elif "Unsupported URL" in error_message:
            return jsonify({'error': 'Unsupported video URL. Please check if it\'s a valid YouTube video.'}), 400
        elif "Requested format is not available" in error_message:
             return jsonify({'error': 'The selected format is not available for this video.'}), 400
        else:
            return jsonify({'error': f'Failed to download video: {error_message}'}), 500
    except Exception as e:
        app.logger.critical(f"An unexpected error occurred during download: {str(e)}", exc_info=True)
        return jsonify({'error': f'An unexpected error occurred during download: {str(e)}'}), 500

@app.route('/download_file/<video_id>')
def download_file(video_id):
    app.logger.info(f"Received /download_file request for video_id: {video_id}")
    videos = app.config.get('videos', {})
    data = videos.pop(video_id, None) # Pop to remove from memory after serving

    if not data:
        app.logger.warning(f"Video ID {video_id} not found or already served.")
        abort(404, description="Download file not found or already served.")

    buffer, filename = data
    app.logger.info(f"Serving file {filename} for video_id: {video_id} from BytesIO buffer.")

    # Determine mimetype based on extension, fallback to octet-stream
    mime_type_map = {
        '.mp4': 'video/mp4', '.mp3': 'audio/mpeg', '.avi': 'video/x-msvideo',
        '.mov': 'video/quicktime', '.flv': 'video/x-flv', '.mkv': 'video/x-matroska',
        '.webm': 'video/webm', '.ogg': 'audio/ogg', '.wav': 'audio/wav',
        '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.3gp': 'video/3gpp',
    }
    ext = os.path.splitext(filename)[1].lower()
    mimetype = mime_type_map.get(ext, 'application/octet-stream')

    try:
        response = send_file(buffer, as_attachment=True, download_name=filename, mimetype=mimetype)
        app.logger.info(f"File {filename} sent successfully for video_id: {video_id}.")
        return response
    except Exception as e:
        app.logger.critical(f"Error sending file {filename} for video_id {video_id}: {str(e)}", exc_info=True)
        abort(500, description=f"Error serving file: {str(e)}")

# No specific teardown needed for BytesIO as it's in-memory and managed by GC.
# The app.config['videos'] dictionary will be cleared when the app process exits.

if __name__ == '__main__':
    app.run(debug=True, threaded=True, port=5000)