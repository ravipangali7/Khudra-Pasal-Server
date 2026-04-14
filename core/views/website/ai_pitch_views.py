"""
Proxy for Google Gemini sales-assist chat (API key stays server-side).
"""

from __future__ import annotations

import json
from typing import Any

import requests
from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.authentication import TokenAuthentication

def _gemini_generate_url() -> str:
    model = getattr(settings, "GEMINI_MODEL", None) or "gemini-1.5-flash"
    model = str(model).strip() or "gemini-1.5-flash"
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


SYSTEM_PROMPT_KHUDRAPASAL_PRODUCT = (
    "You are a friendly, knowledgeable sales assistant for Khudrapasal, a trusted Nepali online store. "
    "Your job is to help customers understand products and feel excited to buy them. "
    "When given a product, write a warm, persuasive 2-3 paragraph pitch that highlights the top 2-3 "
    "genuine benefits, connects the product to the customer's everyday life, and ends with a gentle "
    "call-to-action. Keep the tone friendly, not pushy. Answer follow-up questions honestly. "
    "Always respond in the same language the customer uses (Nepali or English). "
    "Never fabricate features not present in the product description."
)

SYSTEM_PROMPT_GEMINI_API = (
    "You are an enthusiastic, precise product specialist for the Google Gemini API and Google AI Studio. "
    "Pitch and explain the ecosystem clearly for developers evaluating or building with Gemini.\n\n"
    "**Models to highlight when relevant:**\n"
    "- Gemini 3.1 Pro — flagship multimodal understanding and strong reasoning.\n"
    "- Gemini 3 Flash — high performance at lower cost than the largest models.\n"
    "- Gemini 3.1 Flash-Lite — cost- and volume-optimized workhorse in the Gemini 3 family.\n"
    "- Nano Banana 2 and Nano Banana Pro — native image generation and editing.\n"
    "- Veo 3.1 — video generation with native audio.\n"
    "- Veo 3.1 Lite — high-speed, cost-effective video generation at scale (mention when the user asks "
    "about video, scale, or budget-friendly video).\n"
    "- Gemini Robotics — vision-language capabilities for robotics and physical-world reasoning.\n\n"
    "**Capabilities to mention when it fits the question:** long context; structured outputs (e.g. JSON); "
    "function calling for agentic workflows; document understanding for large PDFs and files; built-in "
    "tools (e.g. Google Search, URL context, Maps, code execution, computer use); Live API for real-time "
    "voice agents; and thinking/reasoning modes for harder tasks.\n\n"
    "Point people to Google AI Studio for trying prompts and managing API keys, and to the official "
    "Gemini API docs and status page for up-to-date limits and availability. "
    "Do not invent exact pricing, quotas, or release dates — say these vary by account and product and "
    "to check Google’s current documentation. "
    "Match the customer’s language (English or Nepali). Keep answers scannable unless they ask for depth."
)


def _is_khudrapasal_product_thread(normalized: list[dict[str, str]]) -> bool:
    """Threads started from `buildProductAiContext` carry this exact closing line."""
    if not normalized:
        return False
    first = normalized[0].get("content") or ""
    return "Please give me your sales pitch for this product." in first


def _system_prompt_for_thread(normalized: list[dict[str, str]]) -> str:
    return SYSTEM_PROMPT_KHUDRAPASAL_PRODUCT if _is_khudrapasal_product_thread(normalized) else SYSTEM_PROMPT_GEMINI_API

MAX_MESSAGE_CHARS = 12_000
MAX_MESSAGES = 40


def _normalize_chat_messages(raw: list) -> list[dict[str, str]]:
    """
    Merge consecutive turns with the same role (e.g. double user sends) into one message
    so Gemini receives a valid alternating history.
    """
    merged: list[dict[str, str]] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = m.get("content")
        if role not in ("user", "assistant") or not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        if len(text) > MAX_MESSAGE_CHARS:
            return []
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] = f"{merged[-1]['content']}\n\n{text}"
            if len(merged[-1]["content"]) > MAX_MESSAGE_CHARS:
                return []
        else:
            merged.append({"role": role, "content": text})
    return merged


def _extract_text(data: dict[str, Any]) -> str:
    try:
        cands = data.get("candidates") or []
        if not cands:
            return ""
        parts = (cands[0].get("content") or {}).get("parts") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        return "\n".join(t for t in texts if t).strip()
    except (TypeError, AttributeError, KeyError, IndexError):
        return ""


@api_view(["POST"])
@authentication_classes([JWTAuthentication, TokenAuthentication])
@permission_classes([AllowAny])
def ai_pitch(request):
    key = (getattr(settings, "GEMINI_API_KEY", None) or "").strip()
    if not key:
        return Response(
            {
                "detail": (
                    "AI assistant is not configured. Set GEMINI_API_KEY (or GOOGLE_API_KEY) in the "
                    "API server environment or in server/.env, then restart the Django process."
                ),
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    raw = request.data
    messages = raw.get("messages")
    if not isinstance(messages, list) or len(messages) == 0:
        return Response(
            {"detail": "Expected a non-empty `messages` array."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    normalized = _normalize_chat_messages(messages)
    if not normalized:
        return Response(
            {"detail": "No valid messages (check role and content)."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if len(normalized) > MAX_MESSAGES:
        return Response(
            {"detail": "Too many messages."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    contents: list[dict[str, Any]] = []
    for m in normalized:
        role = m["role"]
        text = m["content"]
        if role == "user":
            contents.append({"role": "user", "parts": [{"text": text}]})
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text}]})

    if not contents or contents[0]["role"] != "user":
        return Response(
            {"detail": "Conversation must start with a user message."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    payload = {
        "systemInstruction": {"parts": [{"text": _system_prompt_for_thread(normalized)}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.75,
            "maxOutputTokens": 2048,
        },
    }

    try:
        r = requests.post(
            f"{_gemini_generate_url()}?key={key}",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=60,
        )
    except requests.RequestException as exc:
        return Response(
            {"detail": f"Upstream request failed: {exc!s}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    try:
        data = r.json()
    except json.JSONDecodeError:
        return Response(
            {"detail": "Invalid response from AI provider."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    if r.status_code != 200:
        err = data.get("error") if isinstance(data, dict) else None
        msg = ""
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("status") or "")
        return Response(
            {"detail": msg or f"AI provider error ({r.status_code})."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    text = _extract_text(data)
    if not text:
        return Response(
            {"detail": "Empty AI response."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response({"text": text})
