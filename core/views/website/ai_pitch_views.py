"""
Proxy for Google Gemini sales-assist chat (API key stays server-side).
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.authentication import TokenAuthentication

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent"
)

SYSTEM_PROMPT = (
    "You are a friendly, knowledgeable sales assistant for Khudrapasal, a trusted Nepali online store. "
    "Your job is to help customers understand products and feel excited to buy them. "
    "When given a product, write a warm, persuasive 2-3 paragraph pitch that highlights the top 2-3 "
    "genuine benefits, connects the product to the customer's everyday life, and ends with a gentle "
    "call-to-action. Keep the tone friendly, not pushy. Answer follow-up questions honestly. "
    "Always respond in the same language the customer uses (Nepali or English). "
    "Never fabricate features not present in the product description."
)

MAX_MESSAGE_CHARS = 12_000
MAX_MESSAGES = 40


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
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return Response(
            {"detail": "AI assistant is not configured (missing GEMINI_API_KEY)."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    raw = request.data
    messages = raw.get("messages")
    if not isinstance(messages, list) or len(messages) == 0:
        return Response(
            {"detail": "Expected a non-empty `messages` array."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if len(messages) > MAX_MESSAGES:
        return Response(
            {"detail": "Too many messages."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    contents: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = m.get("content")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if len(text) > MAX_MESSAGE_CHARS:
            return Response(
                {"detail": "Message too long."},
                status=status.HTTP_400_BAD_REQUEST,
            )
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
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.75,
            "maxOutputTokens": 2048,
        },
    }

    try:
        r = requests.post(
            f"{GEMINI_URL}?key={key}",
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
