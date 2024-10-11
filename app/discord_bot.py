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
hero_name_mapping = {}
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

        logger.info(f"Reaction added: {reaction.emoji} by {user.name}, updated votes: ✅ {poll_info['upvotes']}, ❌ {poll_info['downvotes']}")

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

    # Create a future to wait for reactions
    future = asyncio.Future()
    # Store the future and counts in waiting_polls
    waiting_polls[poll_message.id] = {'future': future, 'upvotes': 0, 'downvotes': 0, 'task_id': task_id}

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
        else:
            upvotes = downvotes = 0

        poll_result = {
            'upvotes': upvotes,
            'downvotes': downvotes
        }
        redis_client.set(f"discord_poll_result:{task_id}", json.dumps(poll_result))
        redis_client.expire(f"discord_poll_result:{task_id}", 60)

        # Update the embed based on poll results
        if upvotes > downvotes:
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

bot.run(DISCORD_TOKEN)