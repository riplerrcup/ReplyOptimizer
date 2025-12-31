import json

from google import genai
from google.genai.types import GenerateContentConfig

from ReplyOptimizer.db_functions.users_functions import get_session


STABLE_INSTRUCTION = """
You are a corporate email assistant.
Always respond professionally and concisely.
Return the answer strictly in JSON format:
{"body": "..."}
Do not use emojis or fabricate information.
If the message is promotional, irrelevant, or unrelated to instructions, return {"body": false}.
"""

def build_prompt(email_text: str, conversation, dynamic_role_instruction: str) -> str:
    if not conversation:
        return f"""
            {STABLE_INSTRUCTION}
            
            Consider the following instructions:
            {dynamic_role_instruction}
            
            Customer email:
            {email_text}
        """
    else:
        return f"""
            {STABLE_INSTRUCTION}

            Consider the following instructions:
            {dynamic_role_instruction}
            
            Conversation(in format sender, message, type): {conversation}

            Customer email:
            {email_text}
        """


def gemini_answer(text, conversation, session):
    user = get_session(session)
    if not user:
        return None, "user_not_found"

    prompt = build_prompt(text, conversation, user.get("instructions"))
    prompt_len = len(prompt)

    client = genai.Client(api_key=user.get("gemini_key"))

    try:
        response = client.models.generate_content(
            model="gemini-2.0-pro",
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=400
            )
        )
    except Exception:
        return None, "provider_error"

    raw_text = response.text or ""

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        return None, "invalid_json"

    body = data.get("body")

    if body is False:
        return None, "filtered"

    if not body:
        return None, "empty_body"

    return {
        "body": body,
        "meta": {
            "prompt_chars": prompt_len,
            "response_chars": len(body),
            "conversation_len": len(conversation or []),
            "model": "gemini-2.0-pro",
        }
    }, "success"
