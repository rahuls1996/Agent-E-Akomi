from typing import Dict, List, Optional, Union, Tuple, Literal
import re
import base64
from io import BytesIO
from PIL import Image
import requests
from autogen import OpenAIWrapper, AssistantAgent, ConversableAgent, Agent
from autogen.agentchat.contrib.img_utils import gpt4v_formatter, message_formatter_pil_to_b64
from autogen.code_utils import content_str
from ae.utils.logger import logger

class EnhancedAssistantAgent(AssistantAgent):
    def __init__(
        self,
        name: str,
        system_message: Optional[Union[str, List]] = AssistantAgent.DEFAULT_SYSTEM_MESSAGE,
        llm_config: Optional[Union[Dict, Literal[False]]] = None,
        **kwargs
    ):
        super().__init__(name, system_message, llm_config, **kwargs)
        
        # Override the `generate_oai_reply` method
        self.replace_reply_func(AssistantAgent.generate_oai_reply, self.generate_oai_reply)

    def generate_oai_reply(
        self,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Agent] = None,
        config: Optional[OpenAIWrapper] = None,
    ) -> Tuple[bool, Union[str, Dict, None]]:
        """Generate a reply using autogen.oai."""
        client = self.client if config is None else config
        if client is None:
            return False, None
        if messages is None:
            messages = self._oai_messages[sender]

        # Format messages for OpenAI API
        formatted_messages = self._format_messages_for_openai(self._oai_system_message + messages)
        
        logger.info(f"Formatted messages: {formatted_messages}")
        extracted_response = self._generate_oai_reply_from_client(
            client, formatted_messages, self.client_cache
        )
        logger.info(f"Extracted response: {extracted_response}")
        return (False, None) if extracted_response is None else (True, extracted_response)

    def _format_messages_for_openai(self, messages: List[Dict]) -> List[Dict]:
        """Format messages for OpenAI API with image support."""
        formatted_messages = []
        for message in messages:
            role = message.get('role', 'user')
            content = message.get('content', '')

            if isinstance(content, str):
                # Extract image data and format content
                formatted_content = self._extract_and_format_content(content)
            elif isinstance(content, list):
                # Handle list of content items (text and images)
                formatted_content = []
                for item in content:
                    if isinstance(item, dict) and 'image_url' in item:
                        # Handle image content
                        encoded_image = self._encode_image_from_url(item['image_url']['url'])
                        formatted_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}
                        })
                    elif isinstance(item, str):
                        # Extract image data and format content for string items
                        formatted_content.extend(self._extract_and_format_content(item))
                    else:
                        # Fallback for unexpected content types
                        formatted_content.append({"type": "text", "text": str(item)})
            else:
                # Fallback for unexpected content types
                formatted_content = [{"type": "text", "text": str(content)}]

            formatted_messages.append({
                "role": role,
                "content": formatted_content
            })

        return formatted_messages

    def _extract_and_format_content(self, text: str) -> List[Dict]:
        """Extract image data from text and format content accordingly."""
        formatted_content = []
        parts = re.split(r'(<img [^>]+>)', text)
        
        for part in parts:
            if part.startswith('<img '):
                # Extract image URL from img tag
                match = re.search(r'<img\s+([^>]+)>', part)
                if match:
                    image_url = match.group(1).strip()
                    encoded_image = self._encode_image_from_url(image_url)
                    formatted_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}
                    })
            elif part.strip():
                # Add non-empty text parts
                formatted_content.append({"type": "text", "text": part.strip()})
        
        return formatted_content

    def _encode_image_from_url(self, image_url: str, max_image: int = 512) -> str:
        """Fetch image from URL, resize if necessary, and encode to base64 string."""
        response = requests.get(image_url)
        response.raise_for_status()  # Raise an exception for bad responses

        with Image.open(BytesIO(response.content)) as img:
            width, height = img.size
            max_dim = max(width, height)
            if max_dim > max_image:
                scale_factor = max_image / max_dim
                new_width = int(width * scale_factor)
                new_height = int(height * scale_factor)
                img = img.resize((new_width, new_height))

            buffered = BytesIO()
            img.save(buffered, format="JPEG")
            return base64.b64encode(buffered.getvalue()).decode("utf-8")