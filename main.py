import concurrent.futures as cf
import glob
import io
import os
import time
import base64
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import List, Literal

import gradio as gr
import sentry_sdk
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger
from openai import OpenAI
from promptic import llm
from pydantic import BaseModel, ValidationError
from pypdf import PdfReader
from tenacity import retry, retry_if_exception_type
from mimetypes import guess_type

if sentry_dsn := os.getenv("SENTRY_DSN"):
    sentry_sdk.init(sentry_dsn)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

class DialogueItem(BaseModel):
    text: str
    speaker: Literal["female-1", "male-1", "female-2", "male-2"]

    @property
    def voice(self):
        return {
            "female-1": "onyx",
            "male-1": "alloy",
            "female-2": "fable",
            "male-2": "echo",
        }[self.speaker]

class Dialogue(BaseModel):
    scratchpad: str
    dialogue: List[DialogueItem]

def get_mp3(text: str, voice: str, api_key: str = None, base_url: str = None) -> bytes:
    client = OpenAI(
        api_key=api_key or os.getenv("OPENAI_API_KEY"),
        base_url=base_url or os.getenv("OPENAI_BASE_URL", "https://api.mr5ai.com/v1"),
    )
    with client.audio.speech.with_streaming_response.create(
        model="tts-1",
        voice=voice,
        input=text,
    ) as response:
        with io.BytesIO() as file:
            for chunk in response.iter_bytes():
                file.write(chunk)
            return file.getvalue()

def is_pdf(filename):
    t, _ = guess_type(filename)
    return filename.lower().endswith(".pdf") or (t or "").endswith("pdf")

def is_image(filename):
    t, _ = guess_type(filename)
    image_exts = (".jpg", ".jpeg", ".png")
    return filename.lower().endswith(image_exts) or (t or "").startswith("image")

def extract_text_from_image_via_vision(image_file, openai_api_key=None, openai_base_url=None):
    client = OpenAI(
        api_key=openai_api_key or os.getenv("OPENAI_API_KEY"),
        base_url=openai_base_url or os.getenv("OPENAI_BASE_URL", "https://api.mr5ai.com/v1"),
    )

    with open(image_file, "rb") as f:
        data = f.read()
        mime_type = guess_type(image_file)[0] or "image/png"
        b64 = base64.b64encode(data).decode("utf-8")
        image_url = f"data:{mime_type};base64,{b64}"

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_url,
                        "detail": "auto"
                    }
                },
                {
                    "type": "text",
                    "text": "Extract all the computer-readable text from this image as accurately as possible. Avoid commentary, return only the extracted text."
                },
            ]
        }
    ]

    response = client.chat.completions.create(
        model="gpt-4.1-mini",  # or "gpt-4o"
        messages=messages,
        max_tokens=8192,
        temperature=0,
    )
    return response.choices[0].message.content.strip()

def generate_audio(mode, files, user_text, openai_api_key: str = None, openai_base_url: str = None):
    """Main entrypoint: Combines mode/file/text logic, returns mp3, transcript"""
    if not (os.getenv("OPENAI_API_KEY") or openai_api_key):
        raise gr.Error("OpenAI API key is required")
    if not (os.getenv("OPENAI_BASE_URL") or openai_base_url):
        raise gr.Error("OpenAI Base URL is required")

    # --- Input validation (point 3) ---
    files_present = files is not None and len(files) > 0
    text_present = user_text is not None and user_text.strip() != ""

    if (mode == "File") and files_present and not text_present:
        # process files into full_text
        texts = []
        for file in files:
            if is_pdf(file):
                with Path(file).open("rb") as f:
                    reader = PdfReader(f)
                    text = "\n\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
            elif is_image(file):
                text = extract_text_from_image_via_vision(file, openai_api_key, openai_base_url)
            else:
                raise gr.Error(f"Unsupported file type: {file}. Please upload PDF or image.")
            texts.append(text)
        full_text = "\n\n".join(texts)
    elif (mode == "Text") and text_present and not files_present:
        full_text = user_text.strip()
    elif ((mode == "File" and text_present) or
          (mode == "Text" and files_present) or
          (not files_present and not text_present)):
        raise gr.Error("Please provide either files or text, not both or neither.")
    else:
        raise gr.Error("Unknown error with input selection.")

    @retry(retry=retry_if_exception_type(ValidationError))
    @llm(
        model="gpt-4.1-mini",
        api_key=openai_api_key or os.getenv("OPENAI_API_KEY"),
        base_url=openai_base_url or os.getenv("OPENAI_BASE_URL"),
    )
    def generate_dialogue(text: str) -> Dialogue:
        """
        Your task is to take the input text provided and turn it into an engaging, informative podcast dialogue. The input text may be messy or unstructured, as it could come from a variety of sources like PDFs or web pages. Don't worry about the formatting issues or any irrelevant information; your goal is to extract the key points and interesting facts that could be discussed in a podcast.

        Here is the input text you will be working with:

        <input_text>
        {text}
        </input_text>

        First, carefully read through the input text and identify the main topics, key points, and any interesting facts or anecdotes. Think about how you could present this information in a fun, engaging way that would be suitable for an audio podcast.

        <scratchpad>
        Brainstorm creative ways to discuss the main topics and key points you identified in the input text. Consider using analogies, storytelling techniques, or hypothetical scenarios to make the content more relatable and engaging for listeners.

        Keep in mind that your podcast should be accessible to a general audience, so avoid using too much jargon or assuming prior knowledge of the topic. If necessary, think of ways to briefly explain any complex concepts in simple terms.

        Use your imagination to fill in any gaps in the input text or to come up with thought-provoking questions that could be explored in the podcast. The goal is to create an informative and entertaining dialogue, so feel free to be creative in your approach.

        Write your brainstorming ideas and a rough outline for the podcast dialogue here. Be sure to note the key insights and takeaways you want to reiterate at the end.
        </scratchpad>

        Now that you have brainstormed ideas and created a rough outline, it's time to write the actual podcast dialogue. Aim for a natural, conversational flow between the host and any guest speakers. Incorporate the best ideas from your brainstorming session and make sure to explain any complex topics in an easy-to-understand way.

        <podcast_dialogue>
        Write your engaging, informative podcast dialogue here, based on the key points and creative ideas you came up with during the brainstorming session. Use a conversational tone and include any necessary context or explanations to make the content accessible to a general audience. Use made-up names for the hosts and guests to create a more engaging and immersive experience for listeners. Do not include any bracketed placeholders like [Host] or [Guest]. Design your output to be read aloud -- it will be directly converted into audio.

        Make the dialogue as long and detailed as possible, while still staying on topic and maintaining an engaging flow. Aim to use your full output capacity to create the longest podcast episode you can, while still communicating the key information from the input text in an entertaining way.

        At the end of the dialogue, have the host and guest speakers naturally summarize the main insights and takeaways from their discussion. This should flow organically from the conversation, reiterating the key points in a casual, conversational manner. Avoid making it sound like an obvious recap - the goal is to reinforce the central ideas one last time before signing off.
        </podcast_dialogue>
        """

    llm_output = generate_dialogue(full_text)

    audio = b""
    transcript = ""
    characters = 0

    with cf.ThreadPoolExecutor() as executor:
        futures = []
        for line in llm_output.dialogue:
            transcript_line = f"{line.speaker}: {line.text}"
            future = executor.submit(get_mp3, line.text, line.voice, openai_api_key, openai_base_url)
            futures.append((future, transcript_line))
            characters += len(line.text)

        for future, transcript_line in futures:
            audio_chunk = future.result()
            audio += audio_chunk
            transcript += transcript_line + "\n\n"

    logger.info(f"Generated {characters} characters of audio")

    temporary_directory = "./gradio_cached_examples/tmp/"
    os.makedirs(temporary_directory, exist_ok=True)

    temporary_file = NamedTemporaryFile(
        dir=temporary_directory,
        delete=False,
        suffix=".mp3",
    )
    temporary_file.write(audio)
    temporary_file.close()

    # Clean old files
    for file in glob.glob(f"{temporary_directory}*.mp3"):
        if os.path.isfile(file) and time.time() - os.path.getmtime(file) > 24 * 60 * 60:
            os.remove(file)

    return temporary_file.name, transcript

allowed_extensions = [
    ".pdf", ".jpg", ".jpeg", ".png"
]

desc = Path("description.md").read_text()
foot = Path("footer.md").read_text()
head_html = os.getenv("HEAD", "") + Path("head.html").read_text()

with gr.Blocks(
    title="Mr.🆖 PodcastAI 🎙️🎧",
    theme="ocean",
    head=head_html,
) as demo:

    gr.Markdown(desc)

    mode_radio = gr.Radio(
        choices=["Text", "File"],
        value="Text",
        label="Input mode:",
        info="Choose whether to paste text or upload PDF/image file(s)"
    )

    text_input = gr.Textbox(
        label="Paste text here",
        lines=12,
        max_lines=64,
        visible=True,
        placeholder="Paste or type your source article or notes here."
    )

    file_input = gr.Files(
        label="Upload PDF or image file(s)",
        file_types=allowed_extensions,
        file_count="multiple",
        visible=False
    )

    api_key_input = gr.Textbox(
        label="OpenAI API Key",
        visible=not os.getenv("OPENAI_API_KEY"),
    )

    base_url_input = gr.Textbox(
        label="OpenAI Base URL",
        visible=not os.getenv("OPENAI_BASE_URL"),
    )

    submit_btn = gr.Button("Generate Podcast Audio")

    output_audio = gr.Audio(label="Audio", format="mp3")
    output_text = gr.Textbox(label="Transcript")

    def update_input_visibility(mode):
        return {
            text_input: gr.update(visible=mode=="Text"),
            file_input: gr.update(visible=mode=="File")
        }

    mode_radio.change(
        update_input_visibility,
        inputs=[mode_radio],
        outputs=[text_input, file_input]
    )

    submit_btn.click(
        generate_audio,
        inputs=[mode_radio, file_input, text_input, api_key_input, base_url_input],
        outputs=[output_audio, output_text]
    )

    gr.Markdown(foot)

demo.queue(max_size=20, default_concurrency_limit=20)
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    demo.launch(show_api=False)
