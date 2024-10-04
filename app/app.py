# app.py

import logging
from flask import Flask, request, jsonify, render_template_string
from werkzeug.utils import secure_filename
import boto3
from celery import Celery

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config.from_object('config')

# Set up Celery
def make_celery(app):
    celery = Celery(
        app.import_name,
        broker=app.config['DEV_BROKER_URL'],
        backend=app.config['DEV_RESULT_BACKEND'],  # Optional
    )
    celery.conf.update(app.config)

    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery

celery = make_celery(app)

# Import celery tasks to ensure they are registered
from . import celery_tasks

# Allowed file extensions
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return (
        '.' in filename
        and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
    )

# Routes
@app.route('/')
def index():
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Image Upload</title>
        </head>
        <body>
            <h1>Upload an Image</h1>
            <form method="POST" action="/upload" enctype="multipart/form-data">
                <label for="image">Select image to upload:</label>
                <input type="file" name="image" id="image">
                <input type="submit" value="Upload Image">
            </form>
        </body>
        </html>
    ''')

@app.route('/upload', methods=['POST'])
def upload_image():
    if 'image' not in request.files:
        return jsonify({'error': 'No image part in the request'}), 400

    image = request.files['image']

    if image.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if image and allowed_file(image.filename):
        filename = secure_filename(image.filename)
        file_content = image.read()

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=app.config['AWS_ACCESS_KEY_ID'],
            aws_secret_access_key=app.config['AWS_SECRET_ACCESS_KEY'],
            region_name=app.config['AWS_REGION'],
        )
        s3_client.put_object(
            Bucket=app.config['AWS_S3_BUCKET'], Key=filename, Body=file_content
        )

        return jsonify({'message': 'Image successfully uploaded'}), 200
    else:
        return jsonify({'error': 'File type not allowed'}), 400

# Run the app if executed directly (useful for local testing)
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
