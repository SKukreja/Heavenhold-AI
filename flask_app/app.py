# app.py

import logging
from flask import Flask, request, jsonify, render_template_string
from werkzeug.utils import secure_filename
import boto3
import redis
import json
from config import AWS_S3_BUCKET

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

# Store boto3 config as a variable to re-use
boto3_config = {
    'aws_access_key_id': app.config['AWS_ACCESS_KEY_ID'],
    'aws_secret_access_key': app.config['AWS_SECRET_ACCESS_KEY'],
    'region_name': app.config['AWS_REGION']
}

import uuid

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
    # Initialize Redis client
    redis_client = redis.Redis(host='redis-service', port=6379, db=0)

    # Retrieve cached data
    cached_data = redis_client.get('hero_data')
    dropdown_options = []
    if cached_data is not None:
        hero_data = json.loads(cached_data)
        heroes_list = hero_data['data']['heroes']['nodes']
        dropdown_options = sorted([(hero['slug'], hero['title']) for hero in heroes_list])
        logger.info("Retrieved hero data from cache.")
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Image Upload</title>
        </head>
        <body>
            <h1>Upload an Image</h1>
            <form method="POST" action="/upload" enctype="multipart/form-data">
                <select name="hero_name" id="hero_name">
                    {% for slug, title in dropdown_options %}
                        <option value="{{ slug }}">{{ title }}</option>
                    {% endfor %}
                </select>
                <label for="image">Select image to upload:</label>
                <input type="file" name="image" id="image">
                <input type="submit" value="Upload Image">
            </form>
        </body>
        </html>
    ''', dropdown_options=dropdown_options)

@app.route('/upload', methods=['POST'])
def upload_image():
    global AWS_S3_BUCKET
    if 'image' not in request.files:
        return jsonify({'error': 'No image part in the request'}), 400

    image = request.files['image']
    hero_name = request.form.get('hero_name')

    if image.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if image and allowed_file(image.filename):
        filename = secure_filename(image.filename)
        file_content = image.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        new_filename = f"{hero_name}_{guid}{filename[filename.rfind('.'):]}"

        # Upload the image to S3
        s3_client = boto3.client('s3', **boto3_config)
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"hero-stories/{new_filename}", 
            Body=file_content
        )

        return jsonify({'message': 'Image successfully uploaded'}), 200
    else:
        return jsonify({'error': 'File type not allowed'}), 400

# Run the app if executed directly (useful for local testing)
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
