import logging
import sys
import os
from typing import Literal
import uuid
import discord
import boto3
import requests
from discord.ext import commands
from discord import app_commands
import redis
import json

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# Add parent directory to sys.path
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import DISCORD_TOKEN, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, AWS_S3_BUCKET, GUILD_ID, WORDPRESS_SITE

intents = discord.Intents.default()
intents.message_content = True

# Initialize Redis client
redis_client = redis.Redis(host='redis-service', port=6379, db=0)

dropdown_options = []
hero_name_mapping = {}

def fetch_hero_data():
    global dropdown_options, hero_name_mapping
    # Fetch hero data
    cached_data = redis_client.get('hero_data')
    if cached_data is not None:
        logger.info("Retrieved hero data from cache.")
        hero_data = json.loads(cached_data)
        heroes_list = hero_data['data']['heroes']['nodes']
        # Create a list of (slug, title) tuples
        dropdown_options = sorted([(hero['slug'], hero['title']) for hero in heroes_list], key=lambda x: x[1])
        # Create a mapping for easy lookup
        hero_name_mapping = {hero['slug']: hero['title'] for hero in heroes_list}
    else:
        dropdown_options = []
        hero_name_mapping = {}
        logger.info("No hero data found in Redis.")
    
    return dropdown_options, hero_name_mapping

dropdown_options, hero_name_mapping = fetch_hero_data()

class Lahn(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)        
        # Sync commands for the specific guild
        self.tree.add_command(submit_hero_illustration)
        self.tree.add_command(submit_hero_bio)
        self.tree.add_command(submit_hero_portrait)
        self.tree.add_command(submit_hero_story)
        self.tree.add_command(submit_hero_stats)
        self.tree.add_command(add_new_hero)
        await self.tree.sync(guild=guild)
        logger.info("Bot started successfully.")
        

bot = Lahn()

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user}')

@bot.command(name="manual_sync_commands", hidden=True)
@commands.is_owner()
async def manual_sync_commands(ctx):
    try:
        logger.info("Syncing commands...")         
        ctx.bot.tree.remove_command(submit_hero_story)
        ctx.bot.tree.remove_command(submit_hero_portrait)
        ctx.bot.tree.remove_command(submit_hero_illustration)
        ctx.bot.tree.remove_command(submit_hero_bio)
        ctx.bot.tree.remove_command(submit_hero_stats)
        ctx.bot.tree.remove_command(add_new_hero)
        ctx.bot.tree.add_command(add_new_hero)
        ctx.bot.tree.add_command(submit_hero_stats)
        ctx.bot.tree.add_command(submit_hero_illustration)
        ctx.bot.tree.add_command(submit_hero_bio)
        ctx.bot.tree.add_command(submit_hero_portrait)
        ctx.bot.tree.add_command(submit_hero_story)
        await ctx.bot.tree.sync()
        await ctx.send("Commands synced successfully.")
        logger.info("Commands synced successfully!")
    except Exception as e:
        logger.error(f"Error syncing commands: {e}")
        await ctx.send("Error syncing commands.")

# Define the slash command
@app_commands.command(name="submit_hero_story", description="Upload an image with a hero's story to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image")
async def submit_hero_story(interaction: discord.Interaction, hero: str, image: discord.Attachment):
    dropdown_options, hero_name_mapping = fetch_hero_data()
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        await interaction.followup.send("No hero data found. Please try again later.")
        return
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(hero, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if image is not None:
        filename = image.filename
        file_content = await image.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        file_extension = os.path.splitext(filename)[1]
        new_filename = f"{hero}_{guid}{file_extension}"

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"hero-stories/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Send a confirmation message
        await interaction.followup.send(f"**Hero:** {hero_title}\nStory image uploaded successfully! Revision will be added to queue in 2-3 minutes.")

# Autocomplete function for hero
@submit_hero_story.autocomplete('hero')
async def story_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, trying to reload...")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("There was a problem getting the list of heroes. Please try again later.")
            return
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

# Define the slash command
@app_commands.command(name="submit_hero_portrait", description="Upload an image with a hero's portrait to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image", region="Select a region")
async def submit_hero_portrait(interaction: discord.Interaction, hero: str, image: discord.Attachment, region: Literal['Global', 'Japan']):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(hero, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if image is not None:
        filename = image.filename
        file_content = await image.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        file_extension = os.path.splitext(filename)[1]
        new_filename = f"{hero}_{region}_{guid}{file_extension}"

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"hero-portraits/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Send a confirmation message
        await interaction.followup.send(f"**Hero:** {hero_title}\n**Region:** {region}\nPortrait image uploaded successfully! Revision will be added to queue in 2-3 minutes.")

# Autocomplete function for hero
@submit_hero_portrait.autocomplete('hero')
async def portrait_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("No hero data found. Please try again later.")
            return
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

# Define the slash command
@app_commands.command(name="submit_hero_bio", description="Upload an image with a hero's bio to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image")
async def submit_hero_bio(interaction: discord.Interaction, hero: str, image: discord.Attachment):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(hero, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if image is not None:
        filename = image.filename
        file_content = await image.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        file_extension = os.path.splitext(filename)[1]
        new_filename = f"{hero}_{guid}{file_extension}"

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"hero-bios/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Send a confirmation message
        await interaction.followup.send(f"**Hero:** {hero_title}\nBio image uploaded successfully! Revision will be added to queue in 2-3 minutes.")

# Autocomplete function for hero
@submit_hero_bio.autocomplete('hero')
async def bio_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("No hero data found. Please try again later.")
            return
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

# Define the slash command
@app_commands.command(name="submit_hero_stats", description="Upload an image with a hero's level 100 stats to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image")
async def submit_hero_stats(interaction: discord.Interaction, hero: str, image: discord.Attachment):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(hero, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if image is not None:
        filename = image.filename
        file_content = await image.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        file_extension = os.path.splitext(filename)[1]
        new_filename = f"{hero}_{guid}{file_extension}"

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"hero-stats/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Send a confirmation message
        await interaction.followup.send(f"**Hero:** {hero_title}\nStats image uploaded successfully! Revision will be added to queue in 2-3 minutes.")

# Autocomplete function for hero
@submit_hero_stats.autocomplete('hero')
async def stats_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("No hero data found. Please try again later.")
            return
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

# Define the slash command
@app_commands.command(name="submit_hero_illustration", description="Upload an image with a hero's illustration (no background) to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image")
async def submit_hero_illustration(interaction: discord.Interaction, hero: str, image: discord.Attachment, region: Literal['Global', 'Japan']):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(hero, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if image is not None:
        filename = image.filename
        file_content = await image.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        file_extension = os.path.splitext(filename)[1]
        new_filename = f"{hero}_{region}_{guid}{file_extension}"

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"hero-illustrations/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Send a confirmation message
        await interaction.followup.send(f"**Hero:** {hero_title}\n**Region:** {region}\nIllustration image uploaded successfully! Revision will be added to queue in 2-3 minutes.")

# Autocomplete function for hero
@submit_hero_illustration.autocomplete('hero')
async def illustration_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("No hero data found. Please try again later.")
            return
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

# Define the slash command
@app_commands.command(name="add_new_hero", description="Add a new blank hero to the site.")
@app_commands.describe(name="Enter the hero's full name (with title)")
async def add_new_hero(interaction: discord.Interaction, name: str):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(name, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if hero_title == "Unknown Hero":
        payload = {
            'hero_title': name,
        }
        update_url = WORDPRESS_SITE + '/wp-json/heavenhold/v1/add-new-hero'
        response = requests.post(update_url, data=payload)
        # Raise an exception if the response contains an error
        response.raise_for_status()
        # Send a confirmation message
        await interaction.followup.send(f"**Hero:** {name} created! Hero lists will update in 2-3 minutes.")
    else:
        await interaction.followup.send(f"**Hero:** {name}\nHero already exists.")

bot.run(DISCORD_TOKEN)