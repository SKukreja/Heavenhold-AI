import asyncio
import logging
import sys
import io
from io import BytesIO
import os
from typing import Literal
import uuid
import discord
import boto3
import base64
from PIL import Image
import requests
from discord.ext import commands, tasks
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

from config import DISCORD_TOKEN, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, AWS_S3_BUCKET, GUILD_ID, WORDPRESS_SITE, DISCORD_CHANNEL_ID

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.reactions = True
intents.message_content = True

# Initialize Redis client
redis_client = redis.Redis(host='redis-service', port=6379, db=0)

dropdown_options = []
item_options = []
hero_name_mapping = {}
item_name_mapping = {}
waiting_polls = {}

def decode_base64_to_image(base64_string):
    return io.BytesIO(base64.b64decode(base64_string))

def fetch_hero_data():
    global dropdown_options, hero_name_mapping
    # Fetch hero data
    cached_data = redis_client.get('hero_data')
    if cached_data is not None:
        logger.info("Retrieved hero data from cache.")
        hero_data = json.loads(cached_data)
        
        # Check if the data is a list or has the expected structure
        if isinstance(hero_data, list):
            heroes_list = hero_data  # Use directly if it's a list
        else:
            heroes_list = hero_data.get('data', {}).get('heroes', {}).get('nodes', [])
        
        # Create a list of (slug, title) tuples
        dropdown_options = sorted([(hero['slug'], hero['title']) for hero in heroes_list], key=lambda x: x[1])
        # Create a mapping for easy lookup
        hero_name_mapping = {hero['slug']: hero['title'] for hero in heroes_list}
    else:
        dropdown_options = []
        hero_name_mapping = {}
        logger.info("No hero data found in Redis.")
    
    return dropdown_options, hero_name_mapping

def fetch_item_data():
    global item_options, item_name_mapping
    # Fetch item data
    cached_data = redis_client.get('item_data')
    if cached_data is not None:
        logger.info("Retrieved item data from cache.")
        item_data = json.loads(cached_data)
        
        # Check if the data is a list or has the expected structure
        if isinstance(item_data, list):
            item_list = item_data  # Use directly if it's a list
        else:
            item_list = item_data.get('data', {}).get('items', {}).get('nodes', [])
        
        # Create a list of (slug, title) tuples
        item_options = sorted([(item['slug'], item['title']) for item in item_list], key=lambda x: x[1])
        # Create a mapping for easy lookup
        item_name_mapping = {item['slug']: item['title'] for item in item_list}
    else:
        item_options = []
        item_name_mapping = {}
        logger.info("No item data found in Redis.")
    
    return item_options, item_name_mapping

# Fetch the data
item_options, item_name_mapping = fetch_item_data()
dropdown_options, hero_name_mapping = fetch_hero_data()


class Lahn(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)        
        # Sync commands for the specific guild
        self.tree.add_command(submit_hero_review)
        self.tree.add_command(submit_hero_illustration)
        self.tree.add_command(submit_hero_bio)
        self.tree.add_command(submit_hero_portrait)
        self.tree.add_command(submit_hero_story)
        self.tree.add_command(submit_hero_stats)
        self.tree.add_command(submit_weapon_information)
        self.tree.add_command(submit_merch_information)
        self.tree.add_command(submit_card_information)
        self.tree.add_command(submit_relic_information)
        self.tree.add_command(submit_accessory_information)        
        self.tree.add_command(submit_costume)
        self.tree.add_command(add_new_hero)
        self.tree.add_command(add_new_item)        
        await self.tree.sync()
        logger.info("Bot started successfully.")

bot = Lahn()

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user}')
    check_redis_for_messages.start()

@tasks.loop(seconds=10)
async def check_redis_for_messages():
    try:
        message = redis_client.lpop('discord_message_queue')  # Fetch message from Redis queue
        if message:
            message_data = json.loads(message)
            channel_id = int(message_data['channel_id'])                        
            
            # Check if it's an embed
            if message_data.get('is_embed', False):
                embed_data = message_data['embed']
                task_id = message_data['task_id']  # Task ID for tracking poll result
                image = message_data.get('image', None)
                filename = message_data.get('filename', None)
                await send_embed_to_channel(channel_id, embed_data, task_id, image=image, filename=filename)
            else:
                content = message_data['message']
                await send_message_to_channel(channel_id, content)
    except Exception as e:
        logger.error(f"Error while checking Redis: {e}")

async def send_message_to_channel(channel_id: int, message: str):
    channel = bot.get_channel(channel_id)
    if channel:
        await channel.send(message)
        logger.info(f"Message sent to channel {channel_id}: {message}")
    else:
        logger.error(f"Channel {channel_id} not found")

# Global variables to store upvotes and downvotes for specific tasks
vote_counts = {}

@bot.event
async def on_reaction_add(reaction, user):
    # Ignore the bot's own reactions
    if user == bot.user:
        return

    message_id = reaction.message.id

    if message_id in waiting_polls:
        poll_info = waiting_polls[message_id]
        if reaction.emoji == '✅':
            poll_info['upvotes'] += 1
        elif reaction.emoji == '❌':
            poll_info['downvotes'] += 1
        elif reaction.emoji == '🔄':
            poll_info['retry'] += 1

        logger.info(f"Reaction added: {reaction.emoji} by {user.name}, updated votes: ✅ {poll_info['upvotes']}, ❌ {poll_info['downvotes']}, 🔄 {poll_info.get('retry', 0)}")

        # If at least one reaction received, set the future result to proceed
        if not poll_info['future'].done():
            poll_info['future'].set_result(None)

async def send_embed_to_channel(channel_id: int, embed_data: dict, task_id: str, image: str = None, filename: str = None):
    channel = bot.get_channel(channel_id)
    if not channel:
        logger.error(f"Channel {channel_id} not found")
        return

    redis_client.delete(f"discord_message_queue:{task_id}")
    embed = discord.Embed.from_dict(embed_data)

    if image:
        if not filename:
            filename = 'default_image.png'  # Set a default filename

        # Ensure the filename has the correct extension
        valid_extensions = ('.png', '.jpg', '.jpeg', '.gif')
        if not filename.lower().endswith(valid_extensions):
            filename += '.png'  # Default to .png if no valid extension

        image_bytes = None

        # Decode the base64 image
        try:
            # Remove data URI scheme if present
            if ',' in image:
                image = image.split(',')[1]

            image_data = base64.b64decode(image)
            image_bytes = BytesIO(image_data)
            image_bytes.seek(0)
            image_size = image_bytes.getbuffer().nbytes
            logger.debug(f"Image size: {image_size} bytes")

            if image_size == 0:
                logger.error("Decoded image is empty.")
                return

            if image_size > 8000000:  # 8 MB limit
                logger.warning("Image size exceeds Discord's limit of 8 MB. Resizing and compressing the image.")

                # Open the image using Pillow
                img = Image.open(image_bytes)

                # Resize the image if it's too large, keeping aspect ratio
                max_size = (1800, 1800)
                img.thumbnail(max_size, Image.LANCZOS)  # Use LANCZOS for high-quality downsampling

                # Save the resized image into BytesIO with compression
                compressed_image = BytesIO()
                img_format = img.format if img.format else 'PNG'  # Default to 'PNG' if format is None
                save_kwargs = {'format': img_format}

                # Adjust compression settings based on format
                if img_format in ['JPEG', 'JPG']:
                    save_kwargs['quality'] = 85
                    save_kwargs['optimize'] = True
                elif img_format == 'PNG':
                    save_kwargs['optimize'] = True
                    save_kwargs['compress_level'] = 9

                img.save(compressed_image, **save_kwargs)
                compressed_image.seek(0)

                # Update the image data
                compressed_image_size = compressed_image.getbuffer().nbytes
                logger.debug(f"Compressed image size: {compressed_image_size} bytes")

                if compressed_image_size > 8000000:
                    logger.error("Compressed image still exceeds 8 MB after resizing and compression.")
                    await channel.send("The image is too large to send, even after compression. Please use a smaller image.")
                    return

                image_bytes = compressed_image
            else:
                # Image size is within the limit; no need to resize
                pass

        except Exception as e:
            logger.error(f"Failed to decode or process base64 image: {e}")
            return

        # Create the Discord file and set the image in the embed
        discord_file = discord.File(fp=image_bytes, filename=filename)
        embed.set_image(url=f"attachment://{filename}")
        logger.debug(f"Embed image URL set to: attachment://{filename}")

        # Send the message with the embed and the file
        try:
            poll_message = await channel.send(embed=embed, file=discord_file)
            logger.debug("Message sent successfully with embed and image.")
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return
    else:
        try:
            poll_message = await channel.send(embed=embed)
            logger.debug("Message sent successfully with embed only.")
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return

    await poll_message.add_reaction('✅')
    await poll_message.add_reaction('❌')
    await poll_message.add_reaction('🔄')

    # Create a future to wait for reactions
    future = asyncio.Future()
    # Store the future and counts in waiting_polls
    waiting_polls[poll_message.id] = {'future': future, 'upvotes': 0, 'downvotes': 0, 'retry': 0, 'task_id': task_id}

    try:
        # Wait for the future to be set
        await asyncio.wait_for(future, timeout=60.0)
    except asyncio.TimeoutError:
        logger.info(f"Reaction timeout reached for message ID {poll_message.id}")
    finally:
        # Retrieve vote counts
        poll_info = waiting_polls.pop(poll_message.id, None)
        if poll_info:
            upvotes = poll_info['upvotes']
            downvotes = poll_info['downvotes']
            retry_count = poll_info.get('retry', 0)
        else:
            upvotes = downvotes = 0

        poll_result = {
            'upvotes': upvotes,
            'downvotes': downvotes,
            'retry': retry_count
        }
        redis_client.set(f"discord_poll_result:{task_id}", json.dumps(poll_result))
        redis_client.expire(f"discord_poll_result:{task_id}", 60)

        # Update the embed based on poll results
        if retry_count > 0:
            embed.color = discord.Color.dark_grey()
            embed.set_footer(text="Okay, I'll try again!")
        elif upvotes > downvotes:
            embed.color = discord.Color.green()
            embed.set_footer(text="Thanks for confirming! I'll update the site now.")
        elif downvotes > upvotes:
            embed.color = discord.Color.red()
            embed.set_footer(text="Okay, I won't update the site then.")
        else:
            embed.color = discord.Color.orange()
            embed.set_footer(text="No confirmation received, I'll create a revision.")

        await poll_message.edit(embed=embed)

@bot.command(name="manual_sync_commands", hidden=True)
@commands.is_owner()
async def manual_sync_commands(ctx):
    try:
        logger.info("Syncing commands...")       
        ctx.bot.tree.remove_command(submit_hero_review)  
        ctx.bot.tree.remove_command(submit_hero_story)
        ctx.bot.tree.remove_command(submit_hero_portrait)
        ctx.bot.tree.remove_command(submit_hero_illustration)
        ctx.bot.tree.remove_command(submit_hero_bio)
        ctx.bot.tree.remove_command(submit_hero_stats)
        ctx.bot.tree.remove_command(submit_merch_information)
        ctx.bot.tree.remove_command(submit_card_information)
        ctx.bot.tree.remove_command(submit_relic_information)
        ctx.bot.tree.remove_command(submit_accessory_information)        
        ctx.bot.tree.remove_command(submit_costume)
        ctx.bot.tree.remove_command(add_new_hero)
        ctx.bot.tree.remove_command(add_new_item)
        ctx.bot.tree.add_command(add_new_item)
        ctx.bot.tree.add_command(add_new_hero)
        ctx.bot.tree.add_command(submit_costume)
        ctx.bot.tree.add_command(submit_accessory_information)
        ctx.bot.tree.add_command(submit_relic_information)        
        ctx.bot.tree.add_command(submit_card_information)        
        ctx.bot.tree.add_command(submit_merch_information)        
        ctx.bot.tree.add_command(submit_hero_stats)
        ctx.bot.tree.add_command(submit_hero_illustration)
        ctx.bot.tree.add_command(submit_hero_bio)
        ctx.bot.tree.add_command(submit_hero_portrait)
        ctx.bot.tree.add_command(submit_hero_story)
        ctx.bot.tree.add_command(submit_hero_review)
        await ctx.bot.tree.sync()
        await ctx.send("Commands synced successfully.")
        logger.info("Commands synced successfully!")
    except Exception as e:
        logger.error(f"Error syncing commands: {e}")
        await ctx.send("Error syncing commands.")

@bot.command(name="refresh", hidden=True)
@commands.is_owner()
async def refresh(ctx):
    global dropdown_options, hero_name_mapping, item_options, item_name_mapping
    try:
        logger.info("Refreshing data...")         
        item_options, item_name_mapping = fetch_item_data()
        dropdown_options, hero_name_mapping = fetch_hero_data()
        await ctx.send("Data refreshed.")
        logger.info("Data refreshed.")
    except Exception as e:
        logger.error(f"Error refreshing data: {e}")
        await ctx.send("Error refreshing data.")

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

        # Prepare the embed
        embed = discord.Embed(
            title="Hero Story Uploaded",
            description=f"**Hero:** {hero_title}\nStory image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

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

        # Send a confirmation message with the image
        embed = discord.Embed(
            title="Hero Portrait Uploaded",
            description=f"**Hero:** {hero_title}\nPortrait image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

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

        # Prepare the embed
        embed = discord.Embed(
            title="Hero Bio Uploaded",
            description=f"**Hero:** {hero_title}\nBio image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

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

@app_commands.command(name="submit_hero_stats", description="Upload an image with a hero's level 100 stats to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image")
async def submit_hero_stats(interaction: discord.Interaction, hero: str, image: discord.Attachment):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(hero, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    
    if image is not None:
        filename = image.filename
        file_content = await image.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        file_extension = os.path.splitext(filename)[1]
        new_filename = f"{hero}_{guid}{file_extension}"

        # Upload the image to S3 (optional, if you still want it for storage)
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

        # Prepare the embed
        embed = discord.Embed(
            title="Hero Stats Uploaded",
            description=f"**Hero:** {hero_title}\nStats image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

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

        # Prepare the embed
        embed = discord.Embed(
            title="Hero Illustration Uploaded",
            description=f"**Hero:** {hero_title}\nIllustration image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

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
@app_commands.command(name="submit_weapon_information", description="Upload an image with a weapon's stats or weapon skill to update the site.")
@app_commands.describe(name="The item name", image="Attach an image")
async def submit_weapon_information(interaction: discord.Interaction, name: str, image: discord.Attachment):
    # Get the hero title from the slug
    item_title = item_name_mapping.get(name, "Unknown Item")
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
        new_filename = f"{name}_{guid}{file_extension}"

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"weapon-information/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Prepare the embed
        embed = discord.Embed(
            title="Weapon Information Uploaded",
            description=f"**Item:** {item_title}\nWeapon image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

# Autocomplete function for hero
@submit_weapon_information.autocomplete('name')
async def weapon_information_autocomplete(interaction: discord.Interaction, current: str):
    global item_options
    if item_options is None:
        logger.info("No item data found, trying to reload...")
        item_options, item_name_mapping = fetch_item_data()
        if item_options is None: 
            await interaction.followup.send("There was a problem getting the list of items. Please try again later.")
            return
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in item_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

# Define the slash command
@app_commands.command(name="add_new_hero", description="Add a new blank hero to the site.")
@app_commands.describe(title="Enter the hero's title (Example: 'Super Death Destroyer')", name="Enter the hero's name (Example: 'Bob')")
async def add_new_hero(interaction: discord.Interaction, title: str, name: str):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(title + ' ' + name, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if hero_title == "Unknown Hero":
        payload = {
            'hero_title': title + ' ' + name,
            'hero_name': name,            
        }
        update_url = WORDPRESS_SITE + '/wp-json/heavenhold/v1/add-new-hero'
        response = requests.post(update_url, data=payload)
        # Raise an exception if the response contains an error
        response.raise_for_status()
        # Send a confirmation message
        await interaction.followup.send(f"**Hero:** {name} created! Please allow 2-3 minutes for lists to update.")
    else:
        await interaction.followup.send(f"**Hero:** {name}\nHero already exists.")

# Define the slash command
@app_commands.command(name="add_new_item", description="Add a new blank item to the site.")
@app_commands.describe(name="Enter the item's name (Example: 'Master Sword')")
async def add_new_item(interaction: discord.Interaction, name: str):
    # Get the hero title from the slug
    item_title = item_name_mapping.get(name, "Unknown Item")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if item_title == "Unknown Item":
        payload = {            
            'item_name': name,
        }
        update_url = WORDPRESS_SITE + '/wp-json/heavenhold/v1/add-new-item'
        response = requests.post(update_url, data=payload)
        # Raise an exception if the response contains an error
        response.raise_for_status()
        # Send a confirmation message
        await interaction.followup.send(f"**Item:** {name} created! Please allow 2-3 minutes for lists to update.")
    else:
        await interaction.followup.send(f"**Item:** {name}\nItem already exists.")

@app_commands.command(name="submit_hero_review", description="Update a hero's review on the site.")
@app_commands.describe(hero="Select a hero", message="Provide a Discord message ID to learn from")
async def submit_hero_review(interaction: discord.Interaction, hero: str, message: str):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(hero, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)

    content = (await interaction.channel.fetch_message(int(message))).content
    logger.info(f"Message content: {content}")

    if hero_title != "Unknown Hero":
        # Store the message content in Redis
        redis_client.rpush('hero_review_queue', json.dumps({
            'hero': hero_title,
            'channel_id': interaction.channel.id,
            'message': content,
        }))
        await interaction.followup.send(f"Thanks for submitting information about **{hero_title}**! It will be reviewed shortly.")

# Autocomplete function for hero
@submit_hero_review.autocomplete('hero')
async def review_hero_autocomplete(interaction: discord.Interaction, current: str):
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
@app_commands.command(name="submit_merch_information", description="Update merch equipment on the site.")
@app_commands.describe(name="Enter the item's name (Example: 'Ocarina')")
async def submit_merch_information(interaction: discord.Interaction, name: str):
    # Get the hero title from the slug
    return 

# Define the slash command
@app_commands.command(name="submit_card_information", description="Update card equipment on the site.")
@app_commands.describe(name="Enter the item's name (Example: 'Blue Eyes White Dragon')")
async def submit_card_information(interaction: discord.Interaction, name: str):
    # Get the hero title from the slug
    return 

# Define the slash command
@app_commands.command(name="submit_relic_information", description="Update relic equipment on the site.")
@app_commands.describe(name="Enter the item's name (Example: 'Triforce of Courage')")
async def submit_relic_information(interaction: discord.Interaction, name: str):
    # Get the hero title from the slug
    return 

# Define the slash command
@app_commands.command(name="submit_accessory_information", description="Update accessory equipment on the site.")
@app_commands.describe(name="Enter the item's name (Example: 'Infinity Gauntlet')")
async def submit_accessory_information(interaction: discord.Interaction, name: str):
    # Get the hero title from the slug
    return 

@app_commands.command(name="submit_costume", description="Upload an image with a hero's costume to update the site.")
@app_commands.describe(image="Attach an image of the costume information page", hero="Select a hero for hero costumes", item="Select the costume to update", item_type="Select the associated item type for equipment costumes", illustration="Attach an image of the illustration if it's a super costume")
async def submit_costume(interaction: discord.Interaction, image: discord.Attachment, hero: str='', item: str='', item_type: str='', illustration: discord.Attachment=None):
    isSuper = False
    isEquipment = False
    if hero != '':
        hero_title = hero_name_mapping.get(hero, "Unknown Hero")
        if illustration is not None:
            isSuper = True
    elif item_type != '':
        isEquipment = True

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
        if isEquipment:
            new_filename = f"equipment_{item}_{item_type}_{guid}{file_extension}"
        else:
            new_filename = f"hero_{item}_{hero}_{guid}{file_extension}"

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"costumes/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Send a confirmation message with the image
        embed = discord.Embed(
            title=f"Costume Uploaded",
            description=f"{item} costume uploaded successfully!"
        )

    if isSuper:
        filename = illustration.filename
        file_content = await illustration.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        file_extension = os.path.splitext(filename)[1]
        new_filename = f"hero_{item}_{hero}_{guid}{file_extension}"

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"costume-illustrations/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Send a confirmation message with the image
        embed = discord.Embed(
            title="Super Costume Illustration Uploaded",
            description=f"{hero_title}\n Super Costume illustration uploaded successfully!"
        )

    embed.color = discord.Color.green()

    # Attach the image directly to the Discord message and add it to the embed
    discord_file = discord.File(io.BytesIO(file_content), filename=filename)
    embed.set_image(url=f"attachment://{filename}")

    # Send the embed with the attached image
    await interaction.followup.send(embed=embed, file=discord_file)

@submit_costume.autocomplete('hero')
async def costume_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("No hero data found. Please try again later.")
            return    
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

@submit_costume.autocomplete('item')
async def costume_item_name_autocomplete(interaction: discord.Interaction, current: str):
    global item_options
    if item_options is None:
        logger.info("No item data found, trying to reload...")
        item_options, item_name_mapping = fetch_item_data()
        if item_options is None: 
            await interaction.followup.send("There was a problem getting the list of items. Please try again later.")
            return    
    suggestions = []
    for slug, title in item_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=title))
            if len(suggestions) >= 25:
                break
    return suggestions

@submit_costume.autocomplete('item_type')
async def costume_item_type_name_autocomplete(interaction: discord.Interaction, current: str):
    item_types = [
        { 'id': 'filter-mobile-category-1hsword', 'value': 'one-handed-sword', 'label': 'One-Handed Sword', 'icon': '/icons/equipment/1hsword.webp' },
        { 'id': 'filter-mobile-category-2hsword', 'value': 'two-handed-sword', 'label': 'Two-Handed Sword', 'icon': '/icons/equipment/2hsword.webp' },
        { 'id': 'filter-mobile-category-rifle', 'value': 'rifle', 'label': 'Rifle', 'icon': '/icons/equipment/rifle.webp' },
        { 'id': 'filter-mobile-category-bow', 'value': 'bow', 'label': 'Bow', 'icon': '/icons/equipment/bow.webp' },
        { 'id': 'filter-mobile-category-basket', 'value': 'basket', 'label': 'Basket', 'icon': '/icons/equipment/basket.webp' },
        { 'id': 'filter-mobile-category-staff', 'value': 'staff', 'label': 'Staff', 'icon': '/icons/equipment/staff.webp' },
        { 'id': 'filter-mobile-category-gauntlet', 'value': 'gauntlet', 'label': 'Gauntlet', 'icon': '/icons/equipment/gauntlet.webp' },
        { 'id': 'filter-mobile-category-claw', 'value': 'claw', 'label': 'Claw', 'icon': '/icons/equipment/claw.webp' },
        { 'id': 'filter-mobile-category-shield', 'value': 'shield', 'label': 'Shield', 'icon': '/icons/equipment/shield.webp' },
        { 'id': 'filter-mobile-category-accessory', 'value': 'accessory', 'label': 'Accessory', 'icon': '/icons/equipment/accessory.webp' },
        { 'id': 'filter-mobile-category-costume', 'value': 'costume', 'label': 'Hero Costume', 'icon': '/icons/equipment/herocostume.webp' },
        { 'id': 'filter-mobile-category-equipmentcostume', 'value': 'equipment-costume', 'label': 'Equipment Costume', 'icon': '/icons/equipment/equipmentcostume.webp' },
        { 'id': 'filter-mobile-category-illustrationcostume', 'value': 'illustration-costume', 'label': 'Illustration Costume', 'icon': '/icons/equipment/illustrationcostume.webp' },
        { 'id': 'filter-mobile-category-card', 'value': 'card', 'label': 'Card', 'icon': '/icons/equipment/card.webp' },
        { 'id': 'filter-mobile-category-merch', 'value': 'merch', 'label': 'Merch', 'icon': '/icons/equipment/merch.webp' },
        { 'id': 'filter-mobile-category-relic', 'value': 'relic', 'label': 'Relic', 'icon': '/icons/equipment/relic.webp' },
    ]
    if item_types is None:
        logger.info("No item types found, trying to reload...")
        item_types = fetch_item_data()
        if item_types is None: 
            await interaction.followup.send("There was a problem getting the list of item types. Please try again later.")
            return    
    suggestions = []
    for item_type in item_types:
        if current.lower() in item_type['label'].lower():
            suggestions.append(app_commands.Choice(name=item_type['label'], value=item_type['value']))
            if len(suggestions) >= 25:
                break
    return suggestions

bot.run(DISCORD_TOKEN)