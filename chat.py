#!/root/source/litellm/venv/bin/python3
import os
import sys
import time
import argparse
from google import genai

def get_default_filename(base_name="chat_history", extension="md"):
    counter = 1
    while True:
        filename = f"{base_name}_{counter:03d}.{extension}"
        if not os.path.exists(filename):
            return filename
        counter += 1


# --- Argument Parsing ---
parser = argparse.ArgumentParser(description="Interactive Gemini Chat Session.")
'''
parser.add_argument('--output', type=str, default='chat_result.md',
                    help='Path to the Markdown file where the chat log will be saved (default: chat_result.md).')
'''
parser.add_argument(
    "--output",
    type=str,
    default=get_default_filename(),  # <-- Calls the function here
    help=(
        "Path to the file where the chat log will be saved (default:"
        " chat_history_XXX.txt)."
    ),
)


args = parser.parse_args()

# --- Gemini API Client Setup ---
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("Error: GEMINI_API_KEY environment variable is not set.")
    exit(1)

client = genai.Client(api_key=api_key)

tools = [
    {
        'type': 'code_execution',
    },
    {
        'type': 'google_search',
    },
    {
        'type': 'url_context',
    },
]

print("=" * 50)
print(" Interactive Gemini Agent Chat")
print(f" Conversation log will be saved to: {args.output}")
print(" * Multi-line Paste is supported!")
print(" * Press 'Ctrl+D' (Linux/Mac) or 'Ctrl+Z' then 'Enter' (Windows) to SEND.")
print(" * Press 'Ctrl+C' to exit the script.")
print("=" * 50)

previous_interaction_id = None


def get_multiline_input():
    """Reads multiple lines of input from the user until EOF is reached."""
    print("\nYou (Paste text. Press Ctrl+D/Ctrl+Z to send):")
    lines = []
    while True:
        try:
            line = input()
            lines.append(line)
        except EOFError:
            # Triggered by Ctrl+D (Unix) or Ctrl+Z (Windows)
            break
    return "\n".join(lines).strip()


# --- Main Interactive Loop ---
try:
    while True:
        # Get multi-line user input from the terminal
        user_input = get_multiline_input()

        # Ignore empty inputs
        if not user_input:
            continue

        # Prepare request payload
        create_params = {
            'agent': 'antigravity-preview-05-2026',
            'input': user_input,
            'background': True,
            'tools': tools,
            'environment': {
                'type': 'remote',
                'network': 'disabled',
            },
        }

        # Maintain chat context by linking to the previous interaction
        if previous_interaction_id:
            create_params['previous_interaction_id'] = previous_interaction_id

        # Start timer and interaction
        start_time = time.time()
        interaction = client.interactions.create(**create_params)
        print(f"\nAgent processing... (ID: {interaction.id})")

        # Poll status for the current response
        while True:
            try:
                interaction = client.interactions.get(interaction.id)
            except Exception as e:
                print(f"\nError retrieving interaction status: {e}")
                break

            if interaction.status == "completed":
                time_cost = time.time() - start_time
                response_text = interaction.output_text or ""
                print(f"\nAgent:\n{response_text}")

                # Save interaction ID to maintain conversation context
                previous_interaction_id = interaction.id

                # Append response to the output file
                try:
                    with open(args.output, 'a', encoding='utf-8') as f:
                        f.write(f"### You:\n{user_input}\n\n### Agent:\n{response_text}\n\n---\n\n")
                    print(f"└─ [Appended turn to {args.output} | Time Cost: {time_cost:.2f}s]")
                except Exception as e:
                    print(f"└─ Error saving to file: {e}")
                    print(f"└─ [Time Cost: {time_cost:.2f}s]")

                break

            elif interaction.status == "failed":
                time_cost = time.time() - start_time
                print(f"\nResearch failed: {interaction.error}")
                print(f"└─ [Failed after: {time_cost:.2f}s]")
                break
            else:
                print(f"Research status: {interaction.status}. Waiting...", end='\r')
            time.sleep(5)

except KeyboardInterrupt:
    print("\n\nSession ended by user (Ctrl+C). Goodbye!")
