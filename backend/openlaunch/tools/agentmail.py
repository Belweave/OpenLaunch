from __future__ import annotations

import base64
import json
from typing import Any, Optional
from urllib.parse import quote

from fastapi import Request
from openlaunch.utils.agentmail import AgentMailError, agentmail_request, require_user_inbox
from openlaunch.utils.tool_executor import tool_annotations


async def _call_for_user(
    user: dict,
    method: str,
    path: str,
    *,
    params: Any = None,
    body: Any = None,
) -> str:
    try:
        inbox = await require_user_inbox(user.get('id', ''))
        inbox_id = quote(inbox['inbox_id'], safe='')
        suffix = path.strip('/')
        response = await agentmail_request(
            method,
            f'/v0/inboxes/{inbox_id}' + (f'/{suffix}' if suffix else ''),
            params=params,
            json_body=body,
        )
        if not response.content:
            return json.dumps({'status': True})
        if 'application/json' in response.headers.get('content-type', ''):
            return json.dumps(response.json(), ensure_ascii=False)
        return json.dumps({'content_type': response.headers.get('content-type'), 'size': len(response.content)})
    except AgentMailError as exc:
        return json.dumps({'error': exc.detail, 'status_code': exc.status_code})


@tool_annotations(read_only=True, destructive=False, idempotent=True, external_network=True)
async def list_email_threads(
    limit: int = 20,
    labels: Optional[list[str]] = None,
    page_token: Optional[str] = None,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    List the current user's AgentMail email conversations, newest first.

    :param limit: Maximum conversations to return, from 1 to 100.
    :param labels: Optional labels that every returned thread must have, such as unread or sent.
    :param page_token: Optional continuation token from a previous result.
    :return: JSON containing threads and the next page token.
    """
    params: list[tuple[str, Any]] = [('limit', max(1, min(limit, 100)))]
    params.extend(('labels', label) for label in labels or [])
    if page_token:
        params.append(('page_token', page_token))
    return await _call_for_user(__user__ or {}, 'GET', 'threads', params=params)


@tool_annotations(read_only=True, destructive=False, idempotent=True, external_network=True)
async def search_email(
    query: str,
    limit: int = 20,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    Semantically search the current user's AgentMail messages by sender, recipients, subject, or body.

    :param query: Natural-language or keyword search query.
    :param limit: Maximum matching messages to return, from 1 to 100.
    :return: JSON containing relevance-ranked email messages.
    """
    return await _call_for_user(
        __user__ or {},
        'GET',
        'messages/search',
        params={'q': query, 'limit': max(1, min(limit, 100))},
    )


@tool_annotations(read_only=True, destructive=False, idempotent=True, external_network=True)
async def get_email_thread(
    thread_id: str,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    Get a complete AgentMail conversation, including every message and attachment metadata.

    :param thread_id: AgentMail thread ID returned by list_email_threads or search_email.
    :return: JSON containing the full thread and messages in chronological order.
    """
    return await _call_for_user(__user__ or {}, 'GET', f'threads/{quote(thread_id, safe="")}')


@tool_annotations(read_only=True, destructive=False, idempotent=True, external_network=True)
async def get_email_attachment(
    message_id: str,
    attachment_id: str,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    Download an attachment from the current user's AgentMail inbox. Text attachments are returned as text;
    other files are returned as base64. Results larger than 2 MB must be downloaded from the Email UI.

    :param message_id: AgentMail message ID containing the attachment.
    :param attachment_id: Attachment ID from the message metadata.
    :return: JSON with content_type, size, encoding, and content.
    """
    try:
        inbox = await require_user_inbox((__user__ or {}).get('id', ''))
        response = await agentmail_request(
            'GET',
            f'/v0/inboxes/{quote(inbox["inbox_id"], safe="")}/messages/'
            f'{quote(message_id, safe="")}/attachments/{quote(attachment_id, safe="")}',
        )
        if len(response.content) > 2 * 1024 * 1024:
            return json.dumps(
                {'error': 'Attachment exceeds the 2 MB model-tool limit; use the Email UI to download it'}
            )
        content_type = response.headers.get('content-type', 'application/octet-stream').split(';', 1)[0]
        if content_type.startswith('text/') or content_type in {'application/json', 'application/xml'}:
            encoding = 'utf-8'
            content = response.content.decode('utf-8', errors='replace')
        else:
            encoding = 'base64'
            content = base64.b64encode(response.content).decode('ascii')
        return json.dumps(
            {'content_type': content_type, 'size': len(response.content), 'encoding': encoding, 'content': content},
            ensure_ascii=False,
        )
    except AgentMailError as exc:
        return json.dumps({'error': exc.detail, 'status_code': exc.status_code})


async def send_email(
    to: list[str],
    subject: str,
    text: str,
    html: Optional[str] = None,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    labels: Optional[list[str]] = None,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    Send a new email from the current user's AgentMail address. Confirm recipients and content before calling.

    :param to: Recipient email addresses.
    :param subject: Email subject.
    :param text: Plain-text email body.
    :param html: Optional HTML email body.
    :param cc: Optional carbon-copy recipients.
    :param bcc: Optional blind-carbon-copy recipients.
    :param labels: Optional labels to attach to the sent message.
    :return: JSON describing the sent message.
    """
    body = {'to': to, 'subject': subject, 'text': text}
    for key, value in {'html': html, 'cc': cc, 'bcc': bcc, 'labels': labels}.items():
        if value is not None:
            body[key] = value
    return await _call_for_user(__user__ or {}, 'POST', 'messages/send', body=body)


async def reply_to_email(
    message_id: str,
    text: str,
    html: Optional[str] = None,
    reply_all: bool = False,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    Reply in-thread to an AgentMail message, optionally replying to all participants.

    :param message_id: AgentMail message ID being answered.
    :param text: Plain-text reply body.
    :param html: Optional HTML reply body.
    :param reply_all: When true, reply to all original participants.
    :return: JSON describing the sent reply.
    """
    body = {'text': text}
    if html is not None:
        body['html'] = html
    action = 'reply-all' if reply_all else 'reply'
    return await _call_for_user(__user__ or {}, 'POST', f'messages/{quote(message_id, safe="")}/{action}', body=body)


async def forward_email(
    message_id: str,
    to: list[str],
    text: Optional[str] = None,
    html: Optional[str] = None,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    Forward an AgentMail message and its attachments to new recipients.

    :param message_id: AgentMail message ID to forward.
    :param to: New recipient email addresses.
    :param text: Optional introductory plain-text body.
    :param html: Optional introductory HTML body.
    :param cc: Optional carbon-copy recipients.
    :param bcc: Optional blind-carbon-copy recipients.
    :return: JSON describing the forwarded message.
    """
    body: dict[str, Any] = {'to': to}
    for key, value in {'text': text, 'html': html, 'cc': cc, 'bcc': bcc}.items():
        if value is not None:
            body[key] = value
    return await _call_for_user(__user__ or {}, 'POST', f'messages/{quote(message_id, safe="")}/forward', body=body)


async def update_email_labels(
    message_id: str,
    add_labels: Optional[list[str]] = None,
    remove_labels: Optional[list[str]] = None,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    Update an email's labels. Add read/remove unread after handling mail; add trash to move it to trash.

    :param message_id: AgentMail message ID to update.
    :param add_labels: Labels to add, such as read, resolved, or trash.
    :param remove_labels: Labels to remove, such as unread or trash.
    :return: JSON describing the updated message.
    """
    return await _call_for_user(
        __user__ or {},
        'PATCH',
        f'messages/{quote(message_id, safe="")}',
        body={'add_labels': add_labels or [], 'remove_labels': remove_labels or []},
    )


async def manage_email_draft(
    action: str,
    draft_id: Optional[str] = None,
    draft_json: Optional[str] = None,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    List, create, inspect, update, send, or delete AgentMail drafts, including scheduled and reply drafts.

    :param action: One of list, create, get, update, send, or delete.
    :param draft_id: Required for get, update, send, and delete.
    :param draft_json: JSON object using AgentMail's draft fields for create/update (to, cc, bcc, subject, text, html, labels, attachments, in_reply_to, forward_of, reply_all, send_at, client_id).
    :return: JSON containing drafts or the operation result.
    """
    action = action.lower().strip()
    try:
        body = json.loads(draft_json) if draft_json else None
    except json.JSONDecodeError:
        return json.dumps({'error': 'draft_json must be valid JSON'})
    if action == 'list':
        return await _call_for_user(__user__ or {}, 'GET', 'drafts')
    if action == 'create':
        return await _call_for_user(__user__ or {}, 'POST', 'drafts', body=body or {})
    if action not in {'get', 'update', 'send', 'delete'} or not draft_id:
        return json.dumps({'error': 'Unsupported action or missing draft_id'})
    path = f'drafts/{quote(draft_id, safe="")}'
    if action == 'get':
        return await _call_for_user(__user__ or {}, 'GET', path)
    if action == 'update':
        return await _call_for_user(__user__ or {}, 'PATCH', path, body=body or {})
    if action == 'send':
        return await _call_for_user(__user__ or {}, 'POST', f'{path}/send', body=body)
    return await _call_for_user(__user__ or {}, 'DELETE', path)


async def agentmail_client(
    method: str,
    path: str,
    query_json: Optional[str] = None,
    body_json: Optional[str] = None,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    Access any inbox-scoped AgentMail REST client operation not covered by the dedicated email tools.
    The path is relative to /v0/inboxes/{current_user_inbox}/, for example events,
    webhooks, lists/send/block, metrics/usage, messages/batch-update, or attachments.
    Never include another inbox ID or a full URL.

    :param method: HTTP method: GET, POST, PUT, PATCH, or DELETE.
    :param path: Inbox-relative AgentMail path.
    :param query_json: Optional JSON object of AgentMail query parameters.
    :param body_json: Optional JSON request body matching AgentMail's OpenAPI schema.
    :return: JSON response from AgentMail.
    """
    normalized_method = method.upper()
    if normalized_method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
        return json.dumps({'error': 'Unsupported method'})
    if path.startswith('/') or '..' in path or '://' in path:
        return json.dumps({'error': 'Path must be relative to the current user inbox'})
    try:
        params = json.loads(query_json) if query_json else None
        body = json.loads(body_json) if body_json else None
    except json.JSONDecodeError:
        return json.dumps({'error': 'query_json and body_json must be valid JSON'})
    return await _call_for_user(__user__ or {}, normalized_method, path, params=params, body=body)


for _mutating_tool in (
    send_email,
    reply_to_email,
    forward_email,
    update_email_labels,
    manage_email_draft,
    agentmail_client,
):
    _mutating_tool.__openlaunch_tool_annotations__ = {
        'read_only': False,
        'destructive': True,
        'external_network': True,
        'approval_required': True,
    }
