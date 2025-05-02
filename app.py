from flask import Flask, render_template, request, jsonify, Response
from werkzeug.utils import secure_filename
from PIL import Image
from io import BytesIO
import requests
import time
import threading
import zipfile
import os
import shutil
from flask_socketio import SocketIO, emit
from flask_cors import CORS

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'uploads')
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'tiff', 'webp'}
app.config['SECRET_KEY'] = 'your_secret_key_here'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

# CORS and SocketIO configuration
CORS(app)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# Logo URLs
LOGO_URL = 'https://ilumilamp.net/wp-content/uploads/2025/01/cropped-logo_ilumi.png'

# Create necessary directories
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

@app.after_request
def add_headers(response):
    response.headers['X-Frame-Options'] = 'ALLOW-FROM *'
    response.headers['Content-Security-Policy'] = "frame-ancestors *"
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

def download_logo(logo_url):
    response = requests.get(logo_url, timeout=10)
    return Image.open(BytesIO(response.content)).convert("RGBA")

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def process_images(folder_path, logo_url, callback=None):
    logo = download_logo(logo_url)
    logo_size_center = (300, 150)
    logo_size_bottom_right = (800, 499)
    
    processed_files = 0
    image_files = [f for f in os.listdir(folder_path) if allowed_file(f)]
    total_files = len(image_files)
    
    for filename in image_files:
        image_path = os.path.join(folder_path, filename)
        
        try:
            # Process original image
            image = Image.open(image_path).convert("RGBA")
            
            # Logo at bottom right
            logo_resized_bottom_right = logo.resize(logo_size_bottom_right, Image.LANCZOS)
            position_bottom_right = (image.width - logo_resized_bottom_right.width - 15,
                                   image.height - logo_resized_bottom_right.height - 15)
            image.paste(logo_resized_bottom_right, position_bottom_right, logo_resized_bottom_right)
            
            # Logo at center
            logo_resized_center = logo.resize(logo_size_center, Image.LANCZOS)
            position_center = ((image.width - logo_resized_center.width) // 2,
                             (image.height - logo_resized_center.height) // 2)
            image.paste(logo_resized_center, position_center, logo_resized_center)
            
            # Save image
            if filename.lower().endswith(('.jpg', '.jpeg')):
                image = image.convert("RGB")
            image.save(image_path)
            
            processed_files += 1
            if callback:
                callback(processed_files, total_files)
                
        except Exception as e:
            print(f"Error processing {filename}: {e}")
            continue
    
    return True


@app.route('/')
def index():
    # Add parameter to detect if in iframe
    is_iframe = request.args.get('iframe', False)
    return render_template('index.html', is_iframe=is_iframe)

@app.route('/upload', methods=['POST'])
def upload_files():
    if 'files' not in request.files:
        return jsonify({'error': 'No files uploaded'}), 400
    
    files = request.files.getlist('files')
    if not files or files[0].filename == '':
        return jsonify({'error': 'No files selected'}), 400
    
    # Create unique folder for this upload
    upload_id = f"{int(time.time())}_{len(os.listdir(app.config['UPLOAD_FOLDER']))}"
    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], upload_id)
    os.makedirs(upload_path, exist_ok=True)
    
    # Save files
    saved_files = 0
    for file in files:
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(upload_path, filename))
            saved_files += 1
    
    if saved_files == 0:
        return jsonify({'error': 'No valid files uploaded'}), 400
    
    # Start background processing
    def background_task():
        try:
            def progress_callback(processed, total):
                socketio.emit('progress_update', {
                    'upload_id': upload_id,
                    'progress': int((processed / total) * 100)
                }, namespace='/')
            
            process_images(upload_path, LOGO_URL, progress_callback)
            
            # Send completion notification
            socketio.emit('processing_complete', {
                'upload_id': upload_id,
                'zip_filename': f'ilumi_images_{upload_id}.zip'
            }, namespace='/')
            
        except Exception as e:
            socketio.emit('processing_error', {
                'upload_id': upload_id,
                'error': str(e)
            }, namespace='/')
    
    thread = threading.Thread(target=background_task)
    thread.start()
    
    return jsonify({
        'upload_id': upload_id,
        'message': f"{saved_files} files being processed",
        'total_files': saved_files
    })

@app.route('/download/<upload_id>')
def download_files(upload_id):
    # Security: verify upload_id is valid
    if not upload_id.replace('_', '').isalnum():
        return jsonify({'error': 'Invalid download ID'}), 400
    
    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], upload_id)
    if not os.path.exists(upload_path):
        return jsonify({'error': 'Folder not found'}), 404
    
    # Create ZIP in memory
    memory_file = BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(upload_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.basename(file_path)
                zipf.write(file_path, arcname)
    
    memory_file.seek(0)
    
    # Prepare response
    response = Response(
        memory_file.getvalue(),
        mimetype='application/zip',
        headers={
            'Content-Disposition': f'attachment; filename="ilumi_images_{upload_id}.zip"',
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'no-store'
        }
    )
    
    # Background cleanup
    def cleanup():
        try:
            shutil.rmtree(upload_path)
        except Exception as e:
            app.logger.error(f"Cleanup error: {e}")
    
    threading.Thread(target=cleanup).start()
    
    return response

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
