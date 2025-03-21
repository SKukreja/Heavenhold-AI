story_prompt = '''
"Please analyze this image and generate a JSON object containing only the updated story for this hero. 
The hero's story should be recorded exactly as written, but you will only receive part of it on each screenshot. 
Check the current data for this hero to see if part of the story has already been recorded, and then append or prepend the new parts you see in the image, piecing together as much of the full story as you can in your output. If the current story already has more than what you see, do not change it. 
Respond with only valid JSON data to import. Ensure you include line breaks as <br />, but no more than two should be together. Current data for this hero: "
'''