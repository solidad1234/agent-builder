import json
import requests
import re
import os
import time
import threading
from pathlib import Path
import frappe
from frappe.utils import now_datetime
from frappe.realtime import emit_via_redis, get_user_room


def setup_environment():
    """Set up necessary environment variables and Hermes config from Agent Setup."""
    hermes_home = Path(__file__).resolve().parent.parent.parent / ".hermes"
    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "true"

    agent_setup = frappe.get_doc("Agent Setup")
    api_key = agent_setup.get_password("api_key")
    provider = (agent_setup.provider or "openrouter").strip().lower()
    model = (agent_setup.model or "").strip()

    if not api_key:
        frappe.throw("API key not set in Agent Setup", frappe.ValidationError)

    # Provider name mapping: Agent Setup uses friendly names, Hermes uses internal IDs.
    # "openai" in Hermes is aliased to openrouter — direct OpenAI API access needs "openai-api".
    _provider_hermes_id = {
        "openai":      "openai-api",   # direct api.openai.com — env var: OPENAI_API_KEY
        "openrouter":  "openrouter",   # env var: OPENROUTER_API_KEY
        "anthropic":   "anthropic",    # env var: ANTHROPIC_API_KEY
        "gemini":      "gemini",       # env var: GEMINI_API_KEY
    }
    hermes_provider = _provider_hermes_id.get(provider, provider)

    # Set the provider's env var (e.g. OPENAI_API_KEY, OPENROUTER_API_KEY)
    os.environ[provider.upper() + "_API_KEY"] = api_key

    # Default model per provider when none is specified
    _provider_defaults = {
        "openai":      "gpt-4o-mini",
        "openrouter":  "anthropic/claude-sonnet-4",
        "anthropic":   "claude-3-5-sonnet-latest",
        "gemini":      "gemini-2.5-pro",
    }
    effective_model = model or _provider_defaults.get(provider, "gpt-4o-mini")

    # Write Hermes config.yaml so the framework picks up the right provider/model
    config_path = hermes_home / "config.yaml"
    try:
        import yaml as _yaml
        # Read existing config to preserve other keys (e.g. plugins)
        if config_path.exists():
            with open(config_path, "r") as f:
                config = _yaml.safe_load(f) or {}
        else:
            config = {}

        new_model_config = {
            "provider": hermes_provider,
            "default":  effective_model,
        }

        if config.get("model") != new_model_config:
            config["model"] = new_model_config
            with open(config_path, "w") as f:
                _yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
            
        # Synchronize agent name into SOUL.md
        agent_name = agent_setup.get("agent_name") or "Omnis"
        soul_path = hermes_home / "SOUL.md"
        if soul_path.exists():
            with open(soul_path, "r") as f:
                soul_content = f.read()
                
            import re
            # Match "You are <Name>, an embedded Frappe"
            new_soul_content = re.sub(
                r"(You are )[^,]+(, an embedded Frappe/ERPNext)", 
                rf"\g<1>{agent_name}\g<2>", 
                soul_content
            )
            
            if new_soul_content != soul_content:
                with open(soul_path, "w") as f:
                    f.write(new_soul_content)

    except Exception as e:
        frappe.log_error(title="Hermes Config Write Failed", message=str(e))
   

setup_environment()
from run_agent import AIAgent

@frappe.whitelist()
def new_chat(title=None, message=None):
    """Create a new Agent Chat and return its name."""
    ensure_chat_access()
    doc = frappe.get_doc({
        "doctype": "Agent Chat",
        "title": title or (message[:50] if message else "New Chat"),
        "user": frappe.session.user,
        "status": "Active",
        "last_active": now_datetime(),
        "message_count": 0,
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    
    return {"chat_id": doc.name, "title": doc.title}

@frappe.whitelist()
def get_messages(chat_id, limit=50, start=0):
    """Load display messages for a chat — paginated."""
    chat = frappe.get_doc("Agent Chat", chat_id)
    if chat.user != frappe.session.user:
        frappe.throw("Not authorised", frappe.PermissionError)

    base_fields = ["name", "role", "content", "timestamp"]
    extra_fields = ["attachments", "tool_calls", "is_error"]
    
    query_kwargs = {
        "doctype": "Agent Chat Message",
        "filters": {"chat": chat_id},
        "order_by": "timestamp asc",
        "limit": limit,
        "start": start,
    }

    try:
        messages = frappe.get_list(fields=base_fields + extra_fields, **query_kwargs)
    except Exception:
        messages = frappe.get_list(fields=base_fields, **query_kwargs)
        
    return {"messages": messages, "title": chat.title}

@frappe.whitelist()
def get_chats():
    """Return the current user's chat list."""
    chats = frappe.get_list(
        "Agent Chat",
        filters={"user": frappe.session.user, "status": "Active"},
        fields=["name", "title", "last_active", "message_count"],
        order_by="last_active desc",
        limit=50,
    )
    return {"chats": chats}

def _slugify(value):
    """Turn a skill's display name into a clean /slash-command token."""
    if not value:
        return ""
   
    return re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")

def build_skills_system_prompt(agent_name="Omnis") -> str:
    """
    Build the <available_skills> block for the agent system prompt.
    Similar to Hermes' build_skills_system_prompt().
    """

    skills = frappe.get_all(
        "Skill",
        fields=[
            "name_",
            "description"
        ],
        order_by="name_ asc"
    )

    lines = []
    
    # Add operation guardrails first
    lines.append("<operation_guardrails>")
    lines.append(f"You are a helpful assistant named {agent_name}.")
    lines.append("CRITICAL RESTRICTIONS - You MUST follow these rules:")
    lines.append("")
    lines.append("1. DELETION: You are NOT capable of deleting records from the system.")
    lines.append("   The delete functionality has been intentionally removed.")
    lines.append("   When a user asks to delete a record:")
    lines.append("   - Explain that you cannot delete records")
    lines.append("   - List the available alternatives the USER can do themselves:")
    lines.append("     * Cancel it (if the DocType supports cancellation)")
    lines.append("     * Mark it as disabled (if the DocType has a 'disabled' field)")
    lines.append("     * Update its status to inactive (if the DocType has a status field)")
    lines.append("   - DO NOT perform any of these alternatives yourself")
    lines.append("   - DO NOT call frappe_save_doc to set disabled=1 or status='Disabled'")
    lines.append("   - ONLY explain what options are available for that particular document type")
    lines.append("")
    lines.append("2. DISABLING/INACTIVATING: When a user asks to disable or set a record to inactive:")
    lines.append("   - DO NOT automatically perform the action")
    lines.append("   - Instead, explain to the user how they can do it themselves:")
    lines.append("     * Which field to update (e.g., 'disabled' checkbox)")
    lines.append("     * Where to find it in the UI")
    lines.append("   - ONLY perform the action if the user EXPLICITLY confirms after your explanation")
    lines.append("")
    lines.append("3. WORKAROUNDS: Never attempt workarounds for restricted operations.")
    lines.append("   If you cannot perform an action directly, explain alternatives but DO NOT execute them.")
    lines.append("")
    lines.append("4. CHARTS AND GRAPHS: When a user asks you to plot or render a graph/chart:")
    lines.append("   - Do NOT just provide a markdown table or textual summary.")
    lines.append("   - You MUST output the chart configuration in a ```chart_json block.")
    lines.append("   - The JSON should follow Frappe Charts structure (e.g. {\"data\": {\"labels\": [...], \"datasets\": [{\"values\": [...]}]}, \"type\": \"bar\"}).")
    lines.append("")
    lines.append("5. ACTION CONFIRMATION & OPTIONS: Whenever an operation requires user authorization, clarification, or confirmation before writing/updating changes:")
    lines.append("   - Clearly explain what record and fields will be changed or what choices exist.")
    lines.append("   - You MUST output a ```confirm_json block at the end of your message with this JSON structure:")
    lines.append("     ```confirm_json")
    lines.append("     {")
    lines.append("       \"title\": \"Authorization Required\",")
    lines.append("       \"explanation\": \"Short summary of proposed change\",")
    lines.append("       \"changes\": [{\"field\": \"Field Name\", \"from\": \"Old Value\", \"to\": \"New Value\"}],")
    lines.append("       \"actions\": [")
    lines.append("         {\"label\": \"Option 1\", \"value\": \"Change full_name to Jane Smith\"},")
    lines.append("         {\"label\": \"Option 2\", \"value\": \"Change full_name to Smith\"},")
    lines.append("         {\"label\": \"Cancel\", \"value\": \"CANCEL\", \"action\": \"cancel\"}")
    lines.append("       ]")
    lines.append("     }")
    lines.append("     ```")
    lines.append("   - For standard yes/no confirmations, you may use \"confirm_text\": \"CONFIRM\", \"cancel_text\": \"CANCEL\" instead of an actions array.")
    lines.append("   - ONLY perform the action after explicit user confirmation or selection.")
    lines.append("   - CRITICAL: When the user responds with 'CONFIRM' or selects a confirmation button/option, the operation IS AUTHORIZED. You MUST execute the tool call (e.g. frappe_save_doc, frappe_rename_doc) IMMEDIATELY in that turn. DO NOT ask for confirmation a second time or output another confirmation request.")
    lines.append("")
    lines.append("</operation_guardrails>")
    lines.append("")

    if skills:
        lines.append("<available_skills>")

        for skill in skills:
            name = skill.get("name_")
            description = skill.get("description", "")

            if not name:
                continue

            lines.append(f"- {name}: {description}")

        lines.append("</available_skills>")
        lines.append("")
        lines.append(
            "Before replying, review the available skills above. "
            "If one or more skills appear relevant to the user's request, "
            "call skill_view(skill_name) to load the full instructions before proceeding. "
            "Skills contain workflows, conventions, implementation patterns, "
            "and quality standards specific to this Frappe environment. "
            "Do not assume skill contents; load the skill first when needed."
        )

    return "\n".join(lines)

@frappe.whitelist(allow_guest=False)
def check_chat_access():
    """
    Returns whether the current user is allowed to use the chat widget.
    If 'allowed_role' is set in Agent Setup, only users with that role can access it.
    If blank, all logged-in users can access it.
    """
    try:
        agent_setup = frappe.get_doc("Agent Setup")
        allowed_role = agent_setup.get("allowed_role")

        agent_name = agent_setup.get("agent_name") or "Omnis"

        if not allowed_role:
            # No restriction configured — everyone has access
            return {"has_access": True, "agent_name": agent_name}

        user_roles = frappe.get_roles(frappe.session.user)
        has_access = allowed_role in user_roles
        return {"has_access": has_access, "required_role": allowed_role, "agent_name": agent_name}

    except Exception:
        frappe.log_error(title="Chat Access Check Failed", message=frappe.get_traceback())
        return {"has_access": False}


def ensure_chat_access(user=None):
    """Enforce the configured Agent Setup role at the server boundary."""
    agent_setup = frappe.get_doc("Agent Setup")
    allowed_role = agent_setup.get("allowed_role")
    user = user or frappe.session.user
    if allowed_role and allowed_role not in frappe.get_roles(user):
        frappe.throw("You do not have permission to use the AI assistant.", frappe.PermissionError)


@frappe.whitelist()
def get_skills():
    """Return all skills available to the agent for the frontend."""

    try:
        native_skills = frappe.get_all(
            "Skill",
            fields=[
                "name_",
                "description"
            ],
            order_by="name_ asc"
        )

        formatted_skills = []

        for s in native_skills:
            raw_name = s.get("name_")

            if not raw_name:
                continue

            formatted_skills.append({
                "name": _slugify(raw_name),
                "label": raw_name.replace("-", " ").title().replace(" Ui", " UI"),
                "description": s.get("description", "")
            })

        return {"skills": formatted_skills}

    except Exception:
        frappe.log_error(
            title="Skills Extraction Failed",
            message=frappe.get_traceback()
        )
        return {"skills": []}

def parse_json(data, default=None):
    """Safely parse JSON strings, returning a default if parsing fails."""
    if isinstance(data, str):
        try:
            return json.loads(data)
        except Exception:
            return default if default is not None else []
    return data or (default if default is not None else [])


def get_user_greeting(user):
    """Return a time-based greeting for the authenticated user's first turn."""
    first_name = frappe.db.get_value("User", user, "first_name") or "there"
    first_name = " ".join(str(first_name).split())
    hour = now_datetime().hour

    if hour < 12:
        greeting = "Good morning"
    elif hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    return f"{greeting}, {first_name}. I am your ERP assistant. How can I help you today?"


def is_simple_greeting(message):
    """Return whether the user's message is only a casual greeting."""
    greeting_pattern = (
        r"(?:hi|hello|hey|howdy|greetings|salutations|yo|rada|hyy|sup|hiya|heya|hiyaa|hallo|bonjour|hola|ciao|shalom|namaste|salaam|sawasdee|ahoj|hej|hallochen|guten\s+tag"
        r"(?:good\s+)?(?:morning|afternoon|evening|day))"
        r"[!.?,;:]?\s*(?:there|everyone)?[!.?,;: ]*"
    )
    return bool(re.fullmatch(greeting_pattern, str(message or "").strip(), re.IGNORECASE))


def get_fallback_models(agent_setup, primary_model):
    """Return the configured, valid OpenRouter free-model fallback chain."""
    fallback_models = []
    seen = {primary_model}

    for row in agent_setup.get("fallback_models") or []:
        model_name = (row.get("model") or "").strip()
        if not model_name or model_name in seen:
            continue

        model = frappe.db.get_value(
            "AI Model",
            model_name,
            ["model", "provider", "input_price", "output_price", "supports_tool_calling"],
            as_dict=True,
        )
        if not model:
            continue

        if (
            (model.provider or "").strip().lower() != "openrouter"
            or (model.input_price or 0) != 0
            or (model.output_price or 0) != 0
            or not model.supports_tool_calling
        ):
            continue

        fallback_models.append({"provider": "openrouter", "model": model.model})
        seen.add(model_name)

    return fallback_models


# --- 2. The API Endpoint ---
@frappe.whitelist()
def chat(message, chat_id=None, attachments=None):
    """Queue a message to be sent to the AI agent."""
    user = frappe.session.user
    ensure_chat_access(user)
    attachments = parse_json(attachments, [])

    if not chat_id:
        chat_id = new_chat(message=message).get("chat_id")

    chat_doc = frappe.get_doc("Agent Chat", chat_id)
    if chat_doc.user != user:
        frappe.throw("Not authorized", frappe.PermissionError)

    frappe.enqueue(
        method="agent_builder.api.agent.process_agent_chat",  
        queue="short",
        timeout=300,
        now=frappe.flags.in_test,
        message=message,
        chat_id=chat_id,
        attachments=attachments,
        user=user
    )

    return {"status": "queued", "chat_id": chat_id}


# --- 3. The Background Task ---
def process_agent_chat(message, chat_id, attachments, user):
    """Background job to process the AI agent interaction."""

    ensure_chat_access()

    # Re-run setup on every request so provider/model/key changes in Agent Setup
    # are picked up immediately without restarting workers.
    setup_environment()

    chat_doc = frappe.get_doc("Agent Chat", chat_id)
    room = get_user_room(user)
    agent_context = parse_json(chat_doc.agent_context, None)
    is_first_turn = not agent_context
    user_first_name = frappe.db.get_value("User", user, "first_name") or ""

    agent_message = message
    if attachments:
        file_lines = "\n".join(
            f"- {a.get('file_name', 'file')}: {a.get('file_url', '')}"
            for a in attachments
        )
        agent_message = f"{message}\n\n[Attached files]\n{file_lines}".strip()

    def publish(event, data):
        frappe.publish_realtime(event, data, room=room)

    tool_call_log = []
    _tool_start_times = {}

    def on_token(delta):
        publish("agent_token", {"delta": delta})

    def on_tool_start(tool_call_id, tool_name, args):
        publish("agent_event", {
            "type": "tool_start", "tool": tool_name,
            "args": json.dumps(args), "call_id": tool_call_id
        })
        _tool_start_times[tool_call_id] = time.time()
        tool_call_log.append({
            "call_id": tool_call_id, "tool": tool_name,
            "args": json.dumps(args) if not isinstance(args, str) else args,
            "status": "running",
        })

    def on_tool_done(tool_call_id, tool_name, args, result):
        publish("agent_event", {
            "type": "tool_done", "tool": tool_name,
            "result": str(result)[:500], "call_id": tool_call_id
        })
        started = _tool_start_times.pop(tool_call_id, None)
        for entry in tool_call_log:
            if entry.get("call_id") == tool_call_id:
                entry.update({
                    "status": "done",
                    "elapsed_ms": int((time.time() - started) * 1000) if started else None
                })
                break

    def on_tool_status(event_type, tool_name=None, preview=None, **kwargs):
        payload = {"type": "tool_progress", "event": event_type}
        if tool_name:
            payload["tool"] = tool_name
        if preview:
            payload["preview"] = preview
        publish("agent_event", payload)

    model_attempts = []

    def on_model_status(kind, status_message):
        """Record fallback transitions while exposing only a neutral UI status."""
        message = str(status_message or "")
        lowered = message.lower()
        switching = any(
            phrase in lowered
            for phrase in ("trying fallback", "switching to fallback", "trying a different model")
        )
        if not switching:
            return

        for attempt in reversed(model_attempts):
            if attempt.get("status") == "pending":
                attempt["status"] = "failed"
                rate_limited = "rate" in lowered or "429" in lowered
                attempt["reason"] = "rate_limit" if rate_limited else "provider_error"
                if "429" in lowered:
                    attempt["http_status"] = 429
                break

        next_index = len(model_attempts) - 1
        if next_index < len(fallback_models):
            model_attempts.append({
                "model": fallback_models[next_index]["model"],
                "status": "pending",
            })

        publish("agent_event", {
            "type": "model_status",
            "message": "Trying a different model...",
        })

    def save_chat_message(role, content, extra_fields=None):
        # Guard: content is mandatory in the doctype — use a fallback for
        # tool-only turns where the LLM returns no text.
        if not content or not str(content).strip():
            content = "\u200b"  # zero-width space — satisfies reqd, renders invisible
        doc = {
            "doctype": "Agent Chat Message",
            "chat": chat_id,
            "role": role,
            "content": content,
            "timestamp": now_datetime(),
        }
        if extra_fields:
            doc.update(extra_fields)
        frappe.get_doc(doc).insert(ignore_permissions=True)

    try:
        agent_setup = frappe.get_doc("Agent Setup")
        agent_name = agent_setup.get("agent_name") or "Omnis"

        save_chat_message(
            "user", message,
            {"attachments": json.dumps(attachments)} if attachments else None
        )
        skills_prompt = build_skills_system_prompt(agent_name=agent_name)

        msg_upper = str(message or "").strip().upper()
        if msg_upper == "CONFIRM" or msg_upper.startswith("CONFIRM") or "SET VALUE TO" in msg_upper or "OPTION" in msg_upper or "CHANGE " in msg_upper:
            skills_prompt += (
                "\n\n<user_authorization_status>\n"
                "USER AUTHORIZATION GRANTED: The user has explicitly authorized and confirmed the requested database operation. "
                "DO NOT ask for confirmation again. DO NOT output any confirm_json blocks or ask the user to type or send CONFIRM. "
                "You MUST execute the database tool calls (e.g. frappe_save_doc, frappe_rename_doc) IMMEDIATELY in this turn to write the changes, then confirm completion to the user."
                "\n</user_authorization_status>"
            )

        if user_first_name:
            skills_prompt += (
                "\n\n<current_user>\n"
                f"The authenticated user's first name is {user_first_name}. "
                "Use it naturally when appropriate, but do not repeatedly greet the user.\n"
                "</current_user>"
            )
        if is_first_turn:
            skills_prompt += (
                "\n\nThe application will add the time-based greeting to your first response. "
                "Do not add a separate greeting yourself."
            )

        provider = (agent_setup.provider or "openrouter").strip().lower()
        _provider_defaults = {
            "openai":      "gpt-4o-mini",
            "openai-api":  "gpt-4o-mini",
            "openrouter":  "anthropic/claude-sonnet-4",
            "anthropic":   "claude-3-5-sonnet-latest",
            "gemini":      "gemini-2.5-pro",
        }
        model = (agent_setup.model or "").strip() or _provider_defaults.get(provider, "gpt-4o-mini")
        fallback_models = get_fallback_models(agent_setup, model) if provider == "openrouter" else []
        model_attempts.append({"model": model, "status": "pending"})

        agent = AIAgent(
            model=model,
            fallback_model=fallback_models or None,
            quiet_mode=False,
            platform="frappe",
            ephemeral_system_prompt=skills_prompt,
            enabled_toolsets=["frappe_tools", "skills"],
            stream_delta_callback=on_token,
            tool_start_callback=on_tool_start,
            tool_complete_callback=on_tool_done,
            tool_progress_callback=on_tool_status,
            status_callback=on_model_status,
        )

        from agent_builder.api import tool_context
        tool_context.set_request_context(user=user, chat_id=chat_id, user_message=message)

        try:
            result = agent.run_conversation(
                user_message=agent_message,
                conversation_history=agent_context
            )
        finally:
            tool_context.clear_request_context()

        if result.get("failed"):
            for attempt in reversed(model_attempts):
                if attempt.get("status") == "pending":
                    attempt["status"] = "failed"
                    attempt["reason"] = "provider_error"
                    break
            raise Exception(result.get("error") or result.get("final_response") or "Unknown Agent error")

        final_response = result.get("final_response") or ""
        if is_first_turn:
            greeting = get_user_greeting(user)
            if is_simple_greeting(message):
                final_response = greeting
            else:
                final_response = f"{greeting}\n\n{final_response}".strip()
        successful_model = getattr(agent, "model", model)
        for attempt in model_attempts:
            if attempt.get("model") == successful_model:
                attempt["status"] = "success"
                break
        else:
            model_attempts.append({"model": successful_model, "status": "success"})

        model_attempts_json = json.dumps({
            "primary_model": model,
            "successful_model": successful_model,
            "fallback_used": successful_model != model,
            "attempts": model_attempts,
        })

        save_chat_message(
            "assistant", final_response,
            {
                **({"tool_calls": json.dumps(tool_call_log)} if tool_call_log else {}),
                "model_attempts": model_attempts_json,
            }
        )

        frappe.db.set_value("Agent Chat", chat_id, {
            "agent_context": json.dumps(result["messages"], default=str),
            "last_active": now_datetime(),
            "message_count": (chat_doc.message_count or 0) + 2,
        })
        frappe.db.commit()

        publish("agent_done", {"response": final_response, "chat_id": chat_id})

    except Exception as e:
        frappe.log_error(title="Agent Chat Error", message=frappe.get_traceback())

        for entry in tool_call_log:
            if entry.get("status") == "running":
                entry["status"] = "interrupted"

        error_text = "Sorry, something went wrong while processing that request. Please try again."

        try:
            error_extras = {"is_error": 1}
            if tool_call_log:
                error_extras["tool_calls"] = json.dumps(tool_call_log)
            if model_attempts:
                error_extras["model_attempts"] = json.dumps({
                    "primary_model": model_attempts[0].get("model"),
                    "successful_model": None,
                    "fallback_used": len(model_attempts) > 1,
                    "attempts": model_attempts,
                })
            save_chat_message("assistant", error_text, error_extras)
            frappe.db.commit()
        except Exception:
            frappe.log_error(title="Agent Chat Error - DB Save", message=frappe.get_traceback())

        try:
            publish("agent_error", {"response": error_text, "chat_id": chat_id})
        except Exception:
            pass

@frappe.whitelist()
def sync_models():
    if not frappe.has_permission("Agent Setup", "write"):
        frappe.throw("Not permitted", frappe.PermissionError)

    synced_count = 0

    # Get all manually created providers and map their lowercase versions to their actual names
    all_providers = frappe.get_all("AI Provider", pluck="name")
    valid_providers_map = {p.lower(): p for p in all_providers}

    # 1. OpenRouter Models -> all assigned to 'openrouter' provider
    try:
        response = requests.get("https://openrouter.ai/api/v1/models", timeout=15)
        response.raise_for_status()
        data = response.json().get("data", [])
        
        if "openrouter" in valid_providers_map:
            actual_openrouter_name = valid_providers_map["openrouter"]
            for model in data:
                model_id = model.get("id")
                if not model_id: continue
                
                arch = model.get("architecture", {})
                pricing = model.get("pricing", {})
                
                model_data = {
                    "doctype": "AI Model",
                    "model": model_id,
                    "model_name": model.get("name"),
                    "provider": actual_openrouter_name,
                    "context_window": model.get("context_length"),
                    "max_output_tokens": model.get("top_provider", {}).get("max_completion_tokens") or 0,
                    "input_price": float(pricing.get("prompt", 0) or 0) * 1000000,
                    "output_price": float(pricing.get("completion", 0) or 0) * 1000000,
                    "supports_vision": 1 if "image" in arch.get("input_modalities", []) else 0,
                    "supports_tool_calling": 1 if "tools" in model.get("supported_parameters", []) else 0
                }
                
                if not frappe.db.exists("AI Model", model_id):
                    frappe.get_doc(model_data).insert(ignore_permissions=True)
                else:
                    existing_doc = frappe.get_doc("AI Model", model_id)
                    existing_doc.update(model_data)
                    existing_doc.save(ignore_permissions=True)
                synced_count += 1
    except Exception as e:
        frappe.log_error(title="Sync OpenRouter Models Failed", message=str(e))

    # 2. LiteLLM Models -> for other providers
    try:
        litellm_url = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
        response = requests.get(litellm_url, timeout=15)
        response.raise_for_status()
        litellm_data = response.json()
        
        for model_id, details in litellm_data.items():
            if not isinstance(details, dict):
                continue
            
            raw_provider = details.get("litellm_provider", "").lower()
            if not raw_provider and "/" in model_id:
                raw_provider = model_id.split("/")[0].lower()
                
            provider_name = raw_provider
            if raw_provider in ["bedrock", "anthropic"]:
                provider_name = "anthropic"
            elif raw_provider in ["openai", "azure"]:
                provider_name = "openai"
            elif raw_provider in ["vertex_ai", "vertex_ai-language-models", "gemini"]:
                provider_name = "google"
            elif raw_provider == "nvidia" or "nvidia" in model_id.lower():
                provider_name = "nvidia"
                
            if provider_name == "openrouter":
                continue # Already handled
                
            if provider_name in valid_providers_map:
                actual_provider_name = valid_providers_map[provider_name]
                model_data = {
                    "doctype": "AI Model",
                    "model": model_id,
                    "model_name": model_id,
                    "provider": actual_provider_name,
                    "context_window": details.get("max_tokens") or details.get("max_input_tokens") or 0,
                    "max_output_tokens": details.get("max_output_tokens") or 0,
                    "input_price": float(details.get("input_cost_per_token", 0) or 0) * 1000000,
                    "output_price": float(details.get("output_cost_per_token", 0) or 0) * 1000000,
                    "supports_vision": 1 if details.get("supports_vision") else 0,
                    "supports_tool_calling": 1 if details.get("supports_function_calling") else 0
                }
                
                if not frappe.db.exists("AI Model", model_id):
                    frappe.get_doc(model_data).insert(ignore_permissions=True)
                else:
                    existing_doc = frappe.get_doc("AI Model", model_id)
                    existing_doc.update(model_data)
                    existing_doc.save(ignore_permissions=True)
                synced_count += 1
    except Exception as e:
        frappe.log_error(title="Sync LiteLLM Models Failed", message=str(e))

    frappe.db.commit()
    return {"status": "success", "message": f"Successfully synced {synced_count} models."}