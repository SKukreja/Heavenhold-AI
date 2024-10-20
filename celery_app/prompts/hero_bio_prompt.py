bio_prompt = '''
Please analyze this image and generate a JSON object containing values for 'height', 'weight', 'age', 'species', 'role', and 'element' from the 'Hero Information' section in the screenshot. 
We also want the 'rarity' of the hero. The rarity is the number of stars under the right-most image under the "Evolution Stage" header.
Respond with only valid JSON using the mentioned keys, and ignore any icons or other irrelevant information. 
If the existing data for a particular key is more complete than what you find, use the pre-existing value in your JSON response. 

Your response should be structured as shown below:

{    
    "age": int,
    "height": string,
    "weight": string,
    "species": string,
    "role": string,
    "element": string,
    "rarity": string // Either "1 Star", "2 Star", or "3 Star". Count the number of stars under the right-most image under the "Evolution Stage" header, and return the corresponding option exactly as shown in quotes.
}

Current data for this hero:  
'''