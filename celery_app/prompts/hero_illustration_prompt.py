illustration_prompt = '''
Find the face of the character in the attached image and provide a JSON object containing the coordinates (x, y) and dimensions (width, height) of a square crop box that frames the face in the image. The crop box should:

1. Be a 1:1 square framing the entire face of the character.
2. Not exceed 500x500 pixels.
3. Stay within the bounds of the image.

Respond only with the JSON object in the following format:

{
    "x": int,
    "y": int,
    "width": int,
    "height": int
}

Do not include any additional text in your response. If for any reason you cannot do this, simply respond with x=0, y=0, width=500, height=500 as the JSON object.

Determine the resolution of the image you're looking at, and multiply the values by how much the resolution has been downscaled in the version of the image you're looking at, given that the image's original resolution is:
'''