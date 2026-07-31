# ChatterMate Backend — Access Analysis Report

**Repository:** `github.com/chattermate/chattermate.chat`  
**Cloned to:** `~/project/py_examples/chattermate/chattermate.chat/`  
**Framework:** FastAPI (Python) + Socket.IO  
**Date:** 2026-07-29  

---

## Goal

We are building a **concolic testing framework** for the ChatterMate backend.

**What we want to measure:** Given different symbolic inputs to an entrypoint
(e.g. a JWT token, a conversation token, a chat message, a webhook payload),
what external systems does the code reach? What branches does it take? What
access does it gain or fail to gain?

**How we do it:**

1. **Entrypoints** are called with symbolic values for key variables (tokens,
   IDs, messages, webhook payloads).
2. **Target functions** (`@interceptor.target`) are the boundary between the
   application code and external systems — every call to a DB, API, queue,
   filesystem, or network endpoint is intercepted. Their return values are
   automatically wrapped as symbolic, so downstream `if/else` branches record
   path conditions.
3. **`@symbolic_func`** is the escape hatch for functions whose return value
   should be symbolic but isn't reachable through the standard chain (e.g.
   background workers, composite feature gates).
4. The **CoverageChecker** (Z3-backed) takes the recorded path conditions
   and finds concrete values for unexplored branches — telling us exactly
   which inputs unlock new code paths and external access patterns.

**What we're not doing:** Fuzzing, random testing, or brute-force exploration.
We're using the symbolic execution + SMT solver approach to systematically
enumerate all reachable paths through the access control and branching logic.

## 1. Target Functions (External Systems Accessed)

Every unique external system the backend touches, grouped by type:

### 1.1 PostgreSQL (primary DB) — SQLAlchemy ORM

- **Engine:** `app/database.py` — `create_engine(settings.DATABASE_URL)` with QueuePool
- **ORM:** SQLAlchemy declarative models in `app/models/` (~40+ models)
- **Repository pattern:** `app/repositories/` — each model has a repository (ChatRepository, UserRepository, etc.)
- **Used by:** Every API route, every service, every bot/agent tool
- **Vulnerability surface:** All standard SQL queries flow through the ORM, which is safe from injection. But DB-connector tool (below) is a custom path.
- **Target function:** Repository methods returning `Optional[Model]` or `List[Model]` — e.g. `ChatRepository.find_active_session()`, `UserRepository.get_by_id()`, `CustomerRepository.find_by_email()`. Each is a natural `@interceptor.target` because callers branch on `is None`/`not results`.

### 1.2 External PostgreSQL (customer's own DB) — Guardrailed DB Connector

- **File:** `app/services/db_connector_service.py`, `app/services/sql_guardrails.py`
- **Tool:** `GuardrailedDBTools` in `app/tools/guardrailed_db_toolkit.py`
- **Mechanism:** AI agent can execute `list_database_tables()`, `describe_database_table()`, `query_database()` against arbitrary customer databases configured in the "DB Connector" settings
- **Safety layers:** (1) AST-based SQL validation — only SELECT, table allowlist, forced LIMIT, column masking, (2) read-only session with statement timeout, (3) audit log to `db_connector_audit_logs`
- **Entrypoint dependency:** Ticket investigation agent
- **Target function:** `GuardrailedDBTools.run_readonly_query()` at `app/services/db_connector_service.py:226` and `GuardrailedDBTools.query_database()` at `app/tools/guardrailed_db_toolkit.py:224`. The validator `validate_sql()` at `app/services/sql_guardrails.py:219` is a downstream branch target (returns validated or raises).

### 1.3 PostgreSQL (Vector DB — pgvector) — Agent Knowledge / FAQ

- **Files:** `app/knowledge/optimized_pgvector.py`, `app/knowledge/knowledge_base.py`
- **Tool:** `KnowledgeSearchByAgent` in `app/tools/knowledge_search_byagent.py`
- **Mechanism:** Uses `agno`'s PgVector with FastEmbed embedder (local `all-MiniLM-L6-v2`) for similarity search on agent knowledge bases
- **Note:** Shares the same PostgreSQL instance as the primary DB but uses a separate schema (`ai`)
- **Target function:** `KnowledgeSearchByAgent.search_knowledge_base()` at `app/tools/knowledge_search_byagent.py` and `KnowledgeBase.get_knowledge_base()` at `app/knowledge/knowledge_base.py:353`. Agent branches on empty vs non-empty results.

### 1.4 Redis

- **File:** `app/core/redis.py` — `redis.from_url(settings.REDIS_URL)` with optional TLS
- **Used for:** Socket rate limiting (`app/services/socket_rate_limit.py`), public rate limiting, session caching, Shopify cache (agent config cache)
- **Data:** Transient key-value; no long-term persistence
- **Target function:** `allow_request()` at `app/services/public_rate_limit.py:44` and the `socket_rate_limit()` decorator at `app/services/socket_rate_limit.py:52`. Both return bool controlling request acceptance.

### 1.5 S3-Compatible Storage (AWS S3 / MinIO)

- **File:** `app/core/s3.py` — boto3 client with presigned URLs
- **Used for:** File attachments in chat, help center article images, agent avatar/profile images
- **Operations:** `put_object`, `head_object`, `get_object`, `delete_object`, `generate_presigned_url`
- **URL signing:** `sign_s3_url()` produces ephemeral access; stored URLs are unsigned
- **Note:** Supports both virtual-hosted and path-style buckets
- **Target function:** `upload_file_to_s3()` at `app/core/s3.py:159` and `delete_file_from_s3()` at `app/core/s3.py:248`. Both return `str/None` or `bool` respectively — callers branch on success/failure.

### 1.6 Firebase Cloud Messaging

- **File:** `app/services/firebase.py`
- **Mechanism:** Firebase Admin SDK to push notifications to agent mobile devices
- **Data:** User FCM tokens stored in the users table; sends notification + data payload
- **Graceful degradation:** Falls back to dev mode if credentials not found
- **Target function:** `send_firebase_notification()` at `app/services/firebase.py:71`. Calls branch on `initialize_firebase()` success (dev mode fallback).

### 1.7 SMTP (Email)

- **Config:** `settings.SMTP_SERVER/PORT/USERNAME/PASSWORD`
- **Used for:** Sending agent notifications via email (`app/services/notifications.py`), ticket email notifications (`app/services/ticket_email.py`)
- **Inbound email:** Webhook endpoint at `POST /api/v1/webhooks/email/{account_id}` for receiving replies (SendGrid/Brevo/etc.)
- **Target function:** `notify_user()` at `app/services/notifications.py:44`. Sends email as side effect; branches on SMTP available vs not.

### 1.8 WhatsApp Cloud API (Meta)

- **Files:** `app/channels/whatsapp.py`, `app/services/whatsapp_outbound.py`
- **Adapter pattern:** `app/channels/base.py` defines `ChannelAdapter`; WhatsApp adapter sends outbound messages, processes inbound
- **Outbound:** Message templates, text messages, media via WhatsApp Business API
- **Inbound:** Webhook at `POST /api/v1/webhooks/meta`
- **Target function:** `WhatsAppAdapter.send_text()` at `app/channels/whatsapp.py:118` and `WhatsAppAdapter.parse_inbound()` at `app/channels/whatsapp.py:37`. The base adapter interface `send_text`/`send_media`/`parse_inbound` is the contract — each channel override is also a target.

### 1.9 Messenger / Instagram (Meta)

- **File:** `app/channels/messenger.py`, `app/channels/instagram.py`, `app/channels/meta_base.py`
- **Same webhook as WhatsApp:** routed by `object` field in the payload
- **OAuth:** Facebook Login for Business to connect a Facebook Page
- **Target function:** Same adapter interface. `MessengerAdapter.send_text()` in `app/channels/messenger.py`, `InstagramAdapter.send_text()` in `app/channels/instagram.py`. Both inherit from `MetaBaseAdapter` in `app/channels/meta_base.py`.

### 1.10 Slack

- **Files:** `app/channels/slack.py`, `app/services/slack_events.py`
- **OAuth:** Slack App installation flow
- **Inbound:** Webhook events (messages, app_mention)
- **Outbound:** Send messages via Slack Web API
- **Target function:** `SlackAdapter.send_text()` and `SlackAdapter.parse_inbound()` in `app/channels/slack.py`. Key branching: event type routing (message vs app_mention vs url_verification).

### 1.11 Telegram

- **File:** `app/channels/telegram.py` (+ webhook in `app/api/webhooks/telegram.py`)
- **Inbound:** Telegram bot webhook
- **Outbound:** Telegram Bot API
- **Target function:** `TelegramAdapter.send_text()` and `TelegramAdapter.parse_inbound()` in `app/channels/telegram.py`.

### 1.12 LINE

- **File:** `app/channels/line.py` (+ webhook in `app/api/webhooks/line.py`)
- **Inbound:** LINE Messaging API webhook
- **Outbound:** LINE Messaging API
- **Target function:** `LINEAdapter.send_text()` and `LINEAdapter.parse_inbound()` in `app/channels/line.py`.

### 1.13 SMS (Twilio / Vonage / MessageBird / Plivo / AWS SNS / Brevo)

- **Files:** `app/channels/sms/` directory — 6 adapters
- **Inbound:** Webhook at POST /webhooks/sms/{account_id} (routed by provider)
- **Outbound:** Via the configured SMS adapter
- **Target function:** `SMSAdapter.send_text()` at `app/channels/sms/adapter.py` (dispatches to provider-specific impls: `twilio.py`, `vonage.py`, `messagebird.py`, `plivo.py`, `sns.py`, `brevo.py`). Provider selection itself is a symbolic branch.

### 1.14 Shopify

- **Files:** `app/services/shopify.py`, `app/services/shopify_auth_service.py`, `app/services/shopify_session.py`, `app/tools/shopify_toolkit.py`
- **Mechanism:** Shopify REST + GraphQL Admin API for store data; Session Token auth for widget
- **Toolkit:** `ShopifyTools` registered for the chat agent — can look up products, orders, customers
- **OAuth:** Shopify OAuth install flow
- **Target function:** `ShopifyClient.get_product()` at `app/services/shopify.py:281`, `ShopifyClient.search_products()` at line 590, and `ShopifyTools` methods at `app/tools/shopify_toolkit.py` (e.g. `list_products()` at line 123, `get_product()` at line 204). Agent branches on found/not-found.

### 1.15 Jira

- **Files:** `app/services/jira.py`, `app/tools/jira_toolkit.py`
- **Mechanism:** Jira REST API for creating/fetching tickets
- **Toolkit:** `JiraTools` registered for the chat agent
- **Target function:** `JiraClient.create_issue()` at `app/services/jira.py:182` and `JiraTools.create_jira_ticket()` at `app/tools/jira_toolkit.py:43`. Agent branches on success/error.

### 1.16 MCP (Model Context Protocol) Servers

- **File:** `app/tools/mcp_manager.py` — `ChatAgentMCPMixin`
- **Mechanism:** Connects to external MCP servers via SSE or Streamable HTTP
- **User-configurable:** Per-agent MCP tool configuration stored in `mcp_tool` table
- **Target function:** `MCPManager._connect_and_register()` at `app/tools/mcp_manager.py:187` and `MCPManager.initialize_mcp_tools()` at line 56. Connection success/failure per MCP server is a branching point.

### 1.17 AI Model Providers

- **File:** `app/core/config.py` (OpenAI, Anthropic, Groq, etc. API keys)
- **Used by:** `app/agents/chat_agent.py` — the agent core that calls the LLM
- **Mechanism:** API keys encrypted at rest via Fernet, decrypted at runtime per request
- **Providers:** OpenAI, Anthropic, Groq, Azure OpenAI, Google Gemini, OpenRouter, etc.
- **Target function:** `ChatAgent.get_response()` at `app/agents/chat_agent.py:1113` and `ChatAgent._get_llm_response_only()` at line 805. Both return `ChatResponse` — the agent branches on `response_content.should_transfer`, `response_content.should_end_chat`, tool call results, etc. Also `test_api_key()` at line 1312 for credential validation.

---

## 2. Entrypoints (HTTP + Socket.IO)

### 2.1 HTTP API Routes (FastAPI)

All mounted under `/api/v1`:

| Prefix | File | Auth | Key Access Types |
|---|---|---|---|
| `/api/v1/chats` | `app/api/chat.py` | JWT or Shopify session token | DB (ORM) |
| `/api/v1/channels` | `app/api/channels/` | JWT | DB, External channel APIs |
| `/api/v1/tickets` | `app/api/tickets.py` | JWT | DB |
| `/api/v1/ticket-db-connectors` | `app/api/ticket_db_connectors.py` | JWT | DB |
| `/api/v1/tickets/webhooks` | `app/api/ticket_webhooks.py` | Webhook secret | DB |
| `/api/v1/webhooks` | `app/api/webhooks/` | Channel-specific | WhatsApp, Messenger, Instagram, Telegram, LINE, SMS, Email, Slack |
| `/api/v1/organizations` | `app/api/organizations.py` | JWT | DB |
| `/api/v1/users` | `app/api/users.py` | JWT | DB |
| `/api/v1/help-center` | `app/api/help_center/` | Mixed (public + JWT) | DB, S3, AI |
| `/api/v1/knowledge` | `app/api/knowledge.py` | JWT | DB, Vector DB, S3 |
| `/api/v1/ai` | `app/api/ai_setup.py` | JWT | DB |
| `/api/v1/agent` | `app/api/agent.py` | JWT | DB |
| `/api/v1/people` | `app/api/people.py` | JWT | DB |
| `/api/v1/mcp-tools` | `app/api/mcp_tool.py` | JWT | DB, External MCP servers |
| `/api/v1/notifications` | `app/api/notification.py` | JWT | DB, Firebase |
| `/api/v1/widgets` | `app/api/widget.py` | JWT | DB |
| `/api/v1/groups` | `app/api/user_groups.py` | JWT | DB |
| `/api/v1/roles` | `app/api/roles.py` | JWT | DB |
| `/api/v1/sessions` | `app/api/session_to_agent.py` | JWT | DB |
| `/api/v1/analytics` | `app/api/analytics.py` | JWT | DB |
| `/api/v1/jira` | `app/api/jira.py` | JWT | DB, Jira API |
| `/api/v1/token` | `app/api/token.py` | None (public) | DB |
| `/api/v1/widget-apps` | `app/api/widget_apps.py` | JWT | DB |
| `/api/v1/shopify` | `app/api/shopify.py` | JWT + OAuth | DB, Shopify API |
| `/api/v1/files` | `app/api/file_upload.py` | JWT | S3, DB |
| `/api/v1/workflow` | `app/api/workflow.py` + `workflow_node.py` | JWT | DB |
| `/health` | `app/main.py` | None | None |
| `/health/help-center-domain` | `app/main.py` | None | Cache/DB (help center host) |

### 2.2 Socket.IO Namespaces

Two namespaces, both on the same socket.io server:

#### `/widget` — Customer-facing widget

| Event | File | Auth | Access Types |
|---|---|---|---|
| `connect` | `widget_chat.py` | Conversation token | DB, AI config |
| `chat` | `widget_chat.py` | Conversation token | DB, AI, S3, CRM, External APIs (via AI agent tools) |
| `end_chat` | `widget_chat.py` | Conversation token | DB |
| `get_chat_history` | `widget_chat.py` | Conversation token | DB, S3 |
| `get_workflow_state` | `widget_chat.py` | Conversation token | DB, AI |
| `proceed_workflow` | `widget_chat.py` | Conversation token | DB, AI |
| `submit_form` | `widget_chat.py` | Conversation token | DB, AI |
| `submit_rating` | `widget_chat.py` | Conversation token | DB |
| `submit_contact_info` | `widget_chat.py` | Conversation token | DB |

#### `/agent` — Human agent dashboard

| Event | File | Auth | Access Types |
|---|---|---|---|
| `connect` | `widget_chat.py` | JWT (access token) | DB |
| `agent_message` | `widget_chat.py` | JWT | DB, S3, External channels |
| `join_room` | `widget_chat.py` | JWT | DB |
| `leave_room` | `widget_chat.py` | JWT | DB |
| `taken_over` | `widget_chat.py` | JWT | DB |

### 2.3 Background Workers (async loops)

| Worker | File | Access Types |
|---|---|---|
| `chat_auto_closer` | `app/workers/chat_auto_closer.py` | DB |
| `faq_processor` | `app/workers/faq_processor.py` | DB, AI |
| `knowledge_processor` | `app/workers/knowledge_processor.py` | DB, S3, AI |
| `ticket_investigator` | `app/workers/ticket_investigator.py` | DB, Guardrailed DB, AI |

---

## 3. Key Entrypoint Variables to Make Symbolic

For concolic testing, the goal is to identify input variables whose different values can lead to different code paths. Here's what matters most:

### 3.1 Auth / Permission Variables (Highest Priority)

These control access gating everywhere:

| Variable | Entrypoint | Why Symbolic |
|---|---|---|
| `token` (JWT) | All HTTP routes via `get_current_user()` | Determines user ID, org ID, role permissions. Fuzzing valid vs invalid, expired, different users, different roles changes every downstream access. |
| `conversation_token` | All `/widget` socket events | Determines customer_id, widget_id, org_id. Different tokens route to different sessions. |
| `user.role.permissions` | All permission-gated routes | Determines whether user can reach `view_all_chats`, `manage_all_chats`, `view_people`, etc. |
| `auth_info['auth_type']` | `/api/v1/chats/` | `"jwt"` vs `"shopify_session"` vs `"shopify"` changes the permission check branch entirely. |

### 3.2 Route/Query Parameters (High Priority)

| Variable | Entrypoint | Why Symbolic |
|---|---|---|
| `session_id` | `GET /api/v1/chats/{session_id}` | Controls which chat session's data is accessed. Different UUIDs → different data or 404. |
| `agent_id` | `GET /api/v1/chats/recent`, socket chat events | Filters by agent; used in many downstream queries |
| `status` | `GET /api/v1/chats/recent` | `"open"`, `"closed"`, `"transferred"` — different WHERE clauses |
| `skip`, `limit` | All paginated endpoints | Boundary values (skip=0, skip=negative, limit=0, limit=101, etc.) |
| `user_name`, `customer_email` | Chat list filters | Different WHERE clause shapes (ILIKE, exact match, None) |
| `date_from`, `date_to` | Chat list filters | Different comparison operators |
| `widget_id` | Socket `connect` | Determines which widget config, which agent, which org |
| `customer_id` | Socket events | Determines which customer's data is accessed |
| `page_url` | Socket `connect` auth | Optional string, recorded in lead capture |

### 3.3 Message / Payload Content (Medium Priority)

| Variable | Entrypoint | Why Symbolic |
|---|---|---|
| `message` (string) | Socket `chat` event | The user's chat message. Length (empty, short, long), encoding (unicode, XSS payloads, SQLi attempts), content type. Flows to the AI agent which can trigger any toolkit (Shopify, Jira, DB, Knowledge, MCP). |
| `rating` (int) | Socket `submit_rating` | Values 1–5 vs out-of-range |
| `form_data` (dict) | Socket `submit_form` | Varies by workflow node config; can contain arbitrary fields |
| `file_data` (base64) | Socket `chat` event, `agent_message` event | File uploads with different content types, sizes, and magic byte signatures |

### 3.4 Webhook Payloads (Medium Priority)

| Variable | Entrypoint | Why Symbolic |
|---|---|---|
| `hub.mode`, `hub.verify_token` | Meta webhook GET | Different values accepted/rejected |
| Meta webhook body | `POST /api/v1/webhooks/meta` | `object` field routes to WhatsApp vs Messenger vs Instagram. Contents vary wildly per channel. |
| `X-Hub-Signature-256` header | Meta webhook POST | Valid/invalid signature → 403 or process |
| Email webhook token query param | `POST /api/v1/webhooks/email/{account_id}` | Valid/invalid per-account token |
| Slack webhook body | `POST /api/v1/webhooks/slack` | Events of different types |
| Telegram update body | `POST /api/v1/webhooks/telegram` | Different message types, formats |

### 3.5 AI Agent State Variables

| Variable | Why Symbolic |
|---|---|
| `session['ai_config'].encrypted_api_key` | Encrypted API key; decryption must succeed or fail |
| `session['ai_config'].model_type` | `AIModelType.CHATTERMATE` vs external — changes code path (message limit check, Groq JSON tool path vs agno structured output) |
| `session['enable_rate_limiting']` | Boolean — turns Redis rate limit on/off per session |
| `session['message_limit_reached']` | Boolean — enterprise message limit gate |
| `session['use_workflow']` | Boolean — routes to WorkflowChatService vs ChatAgent |
| `session['source']` | String — source attribution for lead capture |

---

## 4. Access Flow Summary

```
                     ┌──────────────────────────────┐
                     │      Entrypoints (HTTP/Socket) │
                     │  JWT / Conversation Token /   │
                     │  Webhook Secret               │
                     └──────────┬───────────────────┘
                                │
                     ┌──────────▼───────────────────┐
                     │     Auth Layer (app/core/auth) │
                     │  get_current_user()            │
                     │  authenticate_socket*()        │
                     │  require_permissions()         │
                     │  verify_meta_signature()       │
                     └──────────┬───────────────────┘
                                │
              ┌─────────────────┼────────────────────┐
              │                 │                      │
     ┌────────▼────────┐ ┌─────▼──────┐   ┌──────────▼──────────┐
     │ REST API Routes │ │ Socket     │   │  Webhooks           │
     │ (FastAPI)       │ │ Events     │   │  (External triggers) │
     └────────┬────────┘ └─────┬──────┘   └──────────┬──────────┘
              │                 │                      │
     ┌────────▼────────────────▼──────────────────────▼──────────┐
     │                   Service Layer                            │
     │  ChatAgent / WorkflowChatService / ChannelChatService /   │
     │  TicketService / ShopifyService / JiraService / ...       │
     └────────┬────────────────┬─────────────────────┬──────────┘
              │                │                     │
     ┌────────▼───┐   ┌───────▼──────┐   ┌──────────▼──────────┐
     │ Repositories│   │   Tools     │   │  External APIs       │
     │ (ORM)       │   │ (agno)      │   │  (channels)          │
     │ → DB(Postgres)│ │  → Shopify  │   │  → WhatsApp/Meta     │
     │              │   │  → Jira    │   │  → Slack/Telegram    │
     │              │   │  → DB Conn.│   │  → LINE/SMS/Email   │
     │              │   │  → MCP Svr │   │                     │
     │              │   │  → Knowl.  │   │                     │
     │              │   │  → FAQ     │   │                     │
     └──────────────┘   └───────┬──────┘   └────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │  Infrastructure        │
                    │  Redis │ S3 │ Firebase │
                    │  SMTP │ AI Providers  │
                    └────────────────────────┘
```

---

## 5. Decorated Functions (Targets for Concolic Testing)

In the concolic runtime, functions decorated with `@interceptor.target` have:
1. Their calls intercepted and recorded in the Run (as `call` events)
2. Their return values automatically wrapped as symbolic variables (`SYM_RESULT_*`)
3. All downstream comparisons on those return values tracked as path conditions
4. Their return values included in Z3 declarations for SMT solving

For ChatterMate, these are the functions that should be decorated because their
return values determine downstream behavior:

### 5.1 Auth / Permission Functions (Highest Priority)

These functions determine what a user can see — their return values control
nearly every branch downstream:

| Function | File | Returns | Why Symbolic Return Matters |
|---|---|---|---|
| `get_current_user()` | `app/core/auth.py` | User or None | Determines identity; downstream branches on user_id, org_id, role. **Must be symbolic.** |
| `check_permissions()` | `app/core/auth.py` | bool (True/False) | Gates every privileged route. Return value is compared in `if not check_permissions(...): raise 403` |
| `authenticate_socket*()` | `app/core/auth_utils.py` | (widget_id, org_id, ...) tuple | Determines session context for Socket.IO events |
| `verify_conversation_token()` | `app/core/security.py` | dict or None | Valid/invalid determines access to widget sessions |
| `verify_token()` | `app/core/security.py` | dict or None | Valid/invalid determines JWT session identity |

### 5.2 Data Access Functions

These functions touch external systems and their return values control control flow:

| Function | File | Access Type | Why Symbolic |
|---|---|---|---|
| `get_recent_chats()` | `app/repositories/chat.py` | DB (Postgres) | Returns chat list; branch on count > 0, status, pagination |
| `get_chat_detail()` | `app/repositories/chat.py` | DB (Postgres) | Returns chat or None; downstream branching on result |
| `get_agent()` | `app/repositories/agent.py` | DB (Postgres) | Returns Agent or None; controls rate limiting, workflow flags |
| `get_active_config()` | `app/repositories/ai_config.py` | DB (Postgres) | Returns AI config or None; controls model type, API key |
| `verify_meta_signature()` | `app/channels/meta_base.py` | bool | Webhook auth gate; valid/invalid → 403 or process |
| `verify_webhook_token()` | `app/channels/email.py` | bool | Email webhook auth gate |

### 5.3 AI Agent / Tool Functions (Symbolic Return Priority)

Critical because the AI agent toolkits call external APIs and return values
that control the agent's next action:

| Function | File | Access Type | Why Symbolic |
|---|---|---|---|
| `ChatAgent.chat()` | `app/agents/chat_agent.py` | AI Provider | Returns ChatResponse (message, transfer_to_human, end_chat, etc.). **Everything downstream branches on these fields.** |
| `search_knowledge_base()` | `app/tools/knowledge_search_byagent.py` | Vector DB (pgvector) | Returns search results; agent branches on empty vs non-empty |
| `query_database()` | `app/tools/guardrailed_db_toolkit.py` | Customer DB (Postgres) | Returns query results; agent branches on count, values |
| `list_database_tables()` | `app/tools/guardrailed_db_toolkit.py` | Customer DB (Postgres) | Returns table list; agent branches on availability |
| `get_product()` / `search_products()` | `app/tools/shopify_toolkit.py` | Shopify API | Returns product data; agent branches on found/not found |
| `create_jira_ticket()` | `app/tools/jira_toolkit.py` | Jira API | Returns ticket ID or error; agent branches on success/failure |
| `lookup_product()` | `app/services/shopify.py` | Shopify API | Returns product or None |
| `get_agent_availability()` | `app/services/transfer_agent.py` | DB | Returns available status; controls human transfer flow |
| `check_message_limit()` | `app/enterprise/services/message_limit.py` | DB/Redis | Returns bool; controls whether message is processed or rejected |
| `WorkflowExecutionService.process()` | `app/services/workflow_execution.py` | DB, AI | Returns WorkflowResult; branches on transfer_to_human, end_chat, next_node_id |

### 5.4 Channel Adapter Functions

These determine how outbound messages are delivered and their results control
retry/fallback logic:

| Function | File | Returns | Why Symbolic |
|---|---|---|---|
| `send_message()` (per adapter) | `app/channels/*.py` | bool / delivery status | Success/failure of external message delivery |
| `deliver_to_customer()` | `app/services/message_delivery.py` | DeliveryResult | Controls retry, template fallback |
| `upload_file_to_s3()` | `app/core/s3.py` | str (URL) or None | File upload success; URL returned to caller |
| `send_firebase_notification()` | `app/services/firebase.py` | void (logs success) | Side-effectful — success depends on FCM token validity |

### 5.5 Why Target Functions Are Symbolic by Default

When you decorate a function with `@interceptor.target`, its return value is
automatically wrapped as a `SymbolicInt` or `SymbolicString` with a name like
`SYM_RESULT_<func_name>_<call_idx>`. Downstream code that compares this result
(e.g., `if role == 1:`) records a path condition on the SYM_RESULT variable.

The CoverageChecker then includes SYM_RESULT variables in Z3 declarations and
can generate concrete values for missing branches. For example, if only
`role == 1` (admin) was tested, Z3 finds `SYM_RESULT_get_user_role = 2`
(agent) as the missing path.

### 5.6 Standalone @symbolic_func Decorator

`@symbolic_func` **replaces the function body with a symbolic return value.**
It does not trace through the function's internal logic — it mocks the result.
Use this only when you want to skip computation and treat the return value as
an unknown symbolic variable.

```python
from py_runtime import symbolic_func

@symbolic_func
def is_feature_enabled(org_id: str, feature: str) -> bool:
    # Body is not executed. Returns a symbolic bool instead.
    ...
```

The `@interceptor.target` decorator already includes this behavior, so you
don't need both on the same function.

### 5.7 `@symbolic_func` Candidates

`@symbolic_func` is **rarely useful** in this codebase. Here's why:

**What `@symbolic_func` is not:**
- A way to explore code paths that entrypoints can't reach. If a code path
  isn't reachable from any entrypoint with symbolic args, the right fix is to
  add a new entrypoint with symbolic args — not to mock an internal function.
- A substitute for making background workers into entrypoints. Workers like
  `run_ticket_investigator()` should be called as entrypoints with symbolic
  arguments if their internal branching needs exploration.

**What `@symbolic_func` is:**
- A mocking tool for expensive or complex internal functions whose return
  values drive branching, but whose internal implementation is irrelevant
  to the concolic exploration.
- Only useful when the function is *already reachable* through the standard
  chain (symbolic arg → entrypoint → `@interceptor.target`) but its body
  is doing unnecessary work for the purposes of path exploration.

**Actual candidates: None identified yet.**

Every function that produces a return value used for branching in ChatterMate
is either:
1. An entrypoint (called with symbolic args) — covered naturally.
2. A target function (decorated with `@interceptor.target`) — return value
   is symbolic automatically; downstream branches are covered.
3. A pure computation whose inputs are already symbolic — branching is
   recorded naturally as the symbolic values flow through.
4. In a background worker — should be an entrypoint, not mocked.

If during test development it turns out that a particular function's
implementation is too slow or complex to execute symbolically, that's when
`@symbolic_func` becomes useful — but identify those case-by-case, not
preemptively.


## 6. Testing Strategy Notes

- **Chain depth:** Chat messages can trigger a chain: `Socket → ChatAgent → LLM → Tools → (Shopify/Jira/DB/S3/Redis/etc.)`. A concolic test for the socket input must account for all downstream tool call paths.
- **Auth bypass paths:** The `/widget` namespace uses conversation tokens (not JWT), and the webhook endpoints use channel-specific secrets. These are separate authorization domains that need their own symbolic variables.
- **Multi-tenant isolation:** Most DB queries filter by `organization_id`. Missing org_id filters or incorrect role resolution are high-value bugs to catch.
- **DB-connector variable injection:** The guarded `query_database` tool takes connector name + SQL string — these are parsed by an AST validator. Inputs that could bypass the guardrails (table whitelist, SELECT-only, LIMIT enforcement, column masking) are the most interesting symbolic targets.