"""Local web control panel for Hermes Legion Commander account/executor management."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .account_registry import (
    AccountRegistryError,
    companion_path,
    load_registry,
    normalize_account,
    normalize_role_binding,
    save_registry,
)
from .executor_runtime import run_account_action
from .legion_config import load as load_legion_config

MAX_BODY = 1024 * 1024
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def ensure_base_config(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[legion]\nteam_policy = "hybrid"\n', encoding="utf-8")
    if not path.is_file():
        raise AccountRegistryError(f"config path is not a regular file: {path}")
    return path


def keyring_available() -> bool:
    try:
        import keyring  # type: ignore
        return bool(keyring)
    except Exception:
        return False


def store_keyring_secret(account_id: str, secret: str) -> str:
    if not secret:
        raise AccountRegistryError("secret value is empty")
    try:
        import keyring  # type: ignore
    except ImportError as exc:
        raise AccountRegistryError(
            "Python keyring is not installed; use env: or file: secret references, "
            "or install the optional keyring package"
        ) from exc
    service = "hermes-legion-commander"
    keyring.set_password(service, account_id, secret)
    return f"keyring:{service}/{account_id}"


def _base_roles(config_path: Path) -> list[dict[str, str]]:
    try:
        from .config_toml import loads as toml_loads
        raw = toml_loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    roles = raw.get("roles", []) if isinstance(raw, dict) else []
    rows: list[dict[str, str]] = []
    if isinstance(roles, list):
        for role in roles:
            if isinstance(role, Mapping) and role.get("id"):
                rows.append({"id": str(role["id"]), "objective": str(role.get("objective") or "")})
    return rows


class PanelState:
    def __init__(self, config_path: Path) -> None:
        self.config_path = ensure_base_config(config_path)
        self.registry_path = companion_path(self.config_path)
        self.lock = threading.RLock()
        self.csrf = secrets.token_urlsafe(32)

    def state(self) -> dict[str, Any]:
        with self.lock:
            registry = load_registry(self.registry_path)
            return {
                "schema_version": 1,
                "config_path": str(self.config_path),
                "registry_path": str(self.registry_path),
                "registry": registry,
                "base_roles": _base_roles(self.config_path),
                "keyring_available": keyring_available(),
                "presets": [
                    {"id": "codex-cli", "label": "Codex CLI", "auth": ["oauth", "native"]},
                    {"id": "claude-code", "label": "Claude Code", "auth": ["oauth", "native"]},
                    {"id": "openai-compatible-api", "label": "OpenAI-compatible API", "auth": ["api_key"]},
                    {"id": "anthropic-api", "label": "Anthropic Messages API", "auth": ["api_key"]},
                    {"id": "custom-cli", "label": "Custom CLI", "auth": ["oauth", "native", "api_key"]},
                ],
            }

    def upsert_account(self, row: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_account(row)
        with self.lock:
            registry = load_registry(self.registry_path)
            accounts = registry["accounts"]
            for index, existing in enumerate(accounts):
                if existing["id"] == normalized["id"]:
                    accounts[index] = normalized
                    break
            else:
                accounts.append(normalized)
            return save_registry(self.registry_path, registry)

    def delete_account(self, account_id: str) -> dict[str, Any]:
        with self.lock:
            registry = load_registry(self.registry_path)
            registry["accounts"] = [row for row in registry["accounts"] if row["id"] != account_id]
            for binding in registry["role_bindings"]:
                binding["executors"] = [eid for eid in binding["executors"] if eid != account_id]
            registry["role_bindings"] = [row for row in registry["role_bindings"] if row["executors"]]
            return save_registry(self.registry_path, registry)

    def upsert_role(self, row: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_role_binding(row)
        with self.lock:
            registry = load_registry(self.registry_path)
            roles = registry["role_bindings"]
            for index, existing in enumerate(roles):
                if existing["id"] == normalized["id"]:
                    roles[index] = normalized
                    break
            else:
                roles.append(normalized)
            return save_registry(self.registry_path, registry)

    def delete_role(self, role_id: str) -> dict[str, Any]:
        with self.lock:
            registry = load_registry(self.registry_path)
            registry["role_bindings"] = [row for row in registry["role_bindings"] if row["id"] != role_id]
            return save_registry(self.registry_path, registry)

    def set_keyring_secret(self, account_id: str, secret: str) -> dict[str, Any]:
        with self.lock:
            registry = load_registry(self.registry_path)
            account = next((row for row in registry["accounts"] if row["id"] == account_id), None)
            if account is None:
                raise AccountRegistryError(f"unknown account {account_id!r}")
            if account["auth_kind"] != "api_key":
                raise AccountRegistryError("keyring secret storage is only valid for api_key accounts")
            account["secret_ref"] = store_keyring_secret(account_id, secret)
            save_registry(self.registry_path, registry)
            return {"ok": True, "secret_ref": account["secret_ref"]}

    def validate(self) -> dict[str, Any]:
        with self.lock:
            config = load_legion_config(self.config_path)
            return {
                "ok": True,
                "auth_profiles": len(config.registry.auth_profiles),
                "runtimes": len(config.registry.runtimes),
                "executors": len(config.registry.executors),
                "roles": len(config.roles),
                "campaign_nodes": len(config.campaign.nodes),
            }

    def status(self, account_id: str) -> dict[str, Any]:
        config = load_legion_config(self.config_path)
        return run_account_action(config.registry, account_id, "status", timeout=60, interactive=False)

    def login(self, account_id: str) -> dict[str, Any]:
        config = load_legion_config(self.config_path)
        executor = config.registry.executors.get(account_id)
        if executor is None:
            raise AccountRegistryError(f"unknown account {account_id!r}")
        runtime = config.registry.runtimes[executor.runtime]
        profile = config.registry.auth_profiles[executor.auth_profile]
        if not runtime.login_command:
            raise AccountRegistryError(f"runtime {runtime.id!r} does not expose an OAuth/native login command")

        from .executor_runtime import _account_command, account_environment
        command = _account_command(runtime, executor, profile, "login")
        env = account_environment(profile, executor, runtime)

        kwargs: dict[str, Any] = {"env": env, "cwd": str(self.config_path.parent)}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        process = subprocess.Popen(command, **kwargs)
        return {
            "ok": True,
            "pid": process.pid,
            "command": [command[0], "<redacted-args>"],
            "message": "Native login started. Complete the provider flow in the opened browser/terminal.",
        }


def _html(csrf: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Legion Commander - Accounts</title>
<style>
:root {{color-scheme:light dark;--bg:#0d1117;--panel:#161b22;--panel2:#0f141a;--text:#e6edf3;--muted:#8b949e;--line:#30363d;--accent:#58a6ff;--good:#3fb950;--warn:#d29922;--bad:#f85149}}
@media (prefers-color-scheme:light) {{:root{{--bg:#f6f8fa;--panel:#fff;--panel2:#f6f8fa;--text:#1f2328;--muted:#656d76;--line:#d0d7de;--accent:#0969da;--good:#1a7f37;--warn:#9a6700;--bad:#cf222e}}}}
*{{box-sizing:border-box}}body{{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}}main{{max-width:1180px;margin:auto;padding:24px}}h1{{font-size:24px;margin:0 0 6px}}h2{{font-size:18px;margin:0 0 12px}}.muted{{color:var(--muted)}}.toolbar,.row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}.toolbar{{margin:18px 0}}button{{border:1px solid var(--line);background:var(--panel);color:var(--text);padding:8px 12px;border-radius:8px;cursor:pointer}}button.primary{{background:var(--accent);color:white;border-color:var(--accent)}}button.danger{{color:var(--bad)}}button:disabled{{opacity:.45;cursor:not-allowed}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}}.card{{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:16px}}.badge{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 8px;margin:2px;color:var(--muted)}}.badge.good{{color:var(--good)}}.badge.warn{{color:var(--warn)}}label{{display:block;margin:10px 0 4px;color:var(--muted)}}input,select,textarea{{width:100%;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:8px;padding:8px}}input[type=checkbox]{{width:auto}}dialog{{width:min(760px,94vw);border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--text);padding:20px}}dialog::backdrop{{background:#0008}}.cols{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}@media(max-width:680px){{.cols{{grid-template-columns:1fr}}}}.section{{margin-top:28px}}.status{{min-height:22px;margin:8px 0;color:var(--muted)}}.spacer{{flex:1}}small{{color:var(--muted)}}
</style>
</head>
<body>
<main>
<h1>Hermes Legion Commander</h1>
<div class="muted">Multi-account OAuth/API control plane. Localhost only; raw API keys are never written to Legion configuration.</div>
<div class="toolbar"><button class="primary" id="addAccount">Add agent account</button><button id="addRole">Add role binding</button><button id="validate">Validate configuration</button><span class="spacer"></span><span id="summary" class="muted"></span></div>
<div id="status" class="status" aria-live="polite"></div>
<section class="section"><h2>Agent accounts</h2><div id="accounts" class="grid"></div></section>
<section class="section"><h2>Role bindings</h2><div id="roles" class="grid"></div></section>
<section class="section"><h2>Configuration</h2><div class="card"><div id="paths"></div><p class="muted">The companion JSON contains metadata and secret references only. OAuth credentials remain in each CLI's isolated native store.</p></div></section>
</main>
<dialog id="accountDialog"><h2 id="accountTitle">Add agent account</h2><div class="cols">
<div><label for="accountId">Account / executor ID</label><input id="accountId" placeholder="codex-simulation"></div>
<div><label for="accountLabel">Label</label><input id="accountLabel" placeholder="Codex simulation"></div>
<div><label for="preset">Runtime preset</label><select id="preset"></select></div>
<div><label for="provider">Provider ID</label><input id="provider" placeholder="openai"></div>
<div><label for="authKind">Authentication</label><select id="authKind"><option value="oauth">OAuth / subscription login</option><option value="native">Native runtime auth</option><option value="api_key">API key reference</option></select></div>
<div><label for="email">Email label (optional)</label><input id="email" type="email" placeholder="account@example.com"></div>
<div><label for="model">Model</label><input id="model" value="default"></div>
<div><label for="endpoint">API endpoint</label><input id="endpoint" placeholder="https://provider.example/v1/chat/completions"></div>
<div><label for="secretRef">Secret reference</label><input id="secretRef" placeholder="env:MY_PROVIDER_API_KEY"><small>Use env:, file:, or keyring:. Never paste the raw key here.</small></div>
<div><label for="rolesInput">Role IDs</label><input id="rolesInput" placeholder="simulation-and-demo, reviewer"></div>
<div><label for="roleMode">Role mapping</label><select id="roleMode"><option value="preferred">Preferred</option><option value="allowed">Allowed pool</option></select></div>
<div><label for="priority">Priority (lower runs first)</label><input id="priority" type="number" value="100"></div>
<div><label for="remaining">Subscription remaining %</label><input id="remaining" type="number" min="0" max="100" value="100"></div>
<div><label for="parallel">Max parallel</label><input id="parallel" type="number" min="1" value="1"></div>
<div><label for="guardMode">Repository data guard</label><select id="guardMode"><option value="standard">Standard</option><option value="strict">Strict</option><option value="lockdown">Lockdown</option></select></div>
<div><label for="maxClass">Max data class</label><select id="maxClass"><option value="public">Public</option><option value="internal">Internal</option><option value="confidential">Confidential</option><option value="restricted">Restricted</option></select></div>
</div><label><input id="boundOnly" type="checkbox"> Use this account only for assigned roles</label>
<details id="customCli"><summary>Custom CLI advanced settings</summary><label for="command">Command</label><input id="command" placeholder='my-agent --prompt "{{prompt}}"'><label for="loginCommand">Login command</label><input id="loginCommand" placeholder="my-agent login"><label for="statusCommand">Status command</label><input id="statusCommand" placeholder="my-agent auth status"><label for="promptTransport">Prompt transport</label><select id="promptTransport"><option value="argument">Argument</option><option value="stdin">stdin</option></select><label><input id="sandboxEnforced" type="checkbox"> Runtime enforces filesystem sandbox</label></details>
<div class="row" style="margin-top:16px"><button class="primary" id="saveAccount">Save account</button><button id="cancelAccount">Cancel</button></div></dialog>
<dialog id="roleDialog"><h2>Add role binding</h2><label for="roleId">Role ID</label><input id="roleId" placeholder="simulation-and-demo"><label for="roleObjective">Objective</label><textarea id="roleObjective" rows="3" placeholder="Own simulation and demo development."></textarea><label for="roleBindingMode">Binding mode</label><select id="roleBindingMode"><option value="allowed">Allowed executor pool</option><option value="preferred">Preferred executors with failover</option></select><label for="roleExecutors">Executor IDs</label><input id="roleExecutors" placeholder="codex-sim, claude-sim"><div class="row" style="margin-top:16px"><button class="primary" id="saveRole">Save role binding</button><button id="cancelRole">Cancel</button></div></dialog>
<dialog id="secretDialog"><h2>Store API key in OS keyring</h2><p class="muted">The key is sent only to this localhost process and written to the OS keyring. It is never stored in the Legion JSON/TOML.</p><input id="secretAccountId" type="hidden"><label for="secretValue">API key</label><input id="secretValue" type="password" autocomplete="off"><div class="row" style="margin-top:16px"><button class="primary" id="saveSecret">Store in keyring</button><button id="cancelSecret">Cancel</button></div></dialog>
<script>
const CSRF={json.dumps(csrf)};let state=null;const $=id=>document.getElementById(id);const splitCsv=s=>s.split(',').map(x=>x.trim()).filter(Boolean);
async function api(path,options={{}}){{const opts={{...options,headers:{{'Content-Type':'application/json','X-HLC-CSRF':CSRF,...(options.headers||{{}})}}}};const res=await fetch(path,opts);const body=await res.json().catch(()=>({{error:'Invalid server response'}}));if(!res.ok)throw new Error(body.error||`HTTP ${{res.status}}`);return body}}
function setStatus(msg,bad=false){{$('status').textContent=msg;$('status').style.color=bad?'var(--bad)':'var(--muted)'}}function badge(text,cls=''){{const s=document.createElement('span');s.className=`badge ${{cls}}`;s.textContent=text;return s}}
async function refresh(){{state=await api('/api/state');render()}}function render(){{const reg=state.registry;$('summary').textContent=`${{reg.accounts.length}} accounts · ${{reg.role_bindings.length}} role bindings`;$('paths').textContent=`Base: ${{state.config_path}} · Managed: ${{state.registry_path}}`;renderAccounts(reg.accounts);renderRoles(reg.role_bindings)}}
function renderAccounts(accounts){{const root=$('accounts');root.replaceChildren();if(!accounts.length){{const p=document.createElement('p');p.className='muted';p.textContent='No managed accounts yet.';root.append(p);return}}accounts.forEach(a=>{{const card=document.createElement('article');card.className='card';const top=document.createElement('div');top.className='row';const title=document.createElement('strong');title.textContent=a.account_label||a.id;top.append(title);top.append(badge(a.provider),badge(a.preset),badge(a.auth_kind,a.auth_kind==='api_key'?'warn':'good'));card.append(top);const meta=document.createElement('p');meta.className='muted';meta.textContent=`${{a.id}} · model ${{a.model}}${{a.email?' · '+a.email:''}}`;card.append(meta);const guards=document.createElement('div');guards.append(badge(`guard:${{a.data_guard.mode}}`),badge(`max:${{a.data_guard.max_data_class}}`));if(a.roles?.length)guards.append(badge(`roles:${{a.roles.join(',')}}`));card.append(guards);const actions=document.createElement('div');actions.className='row';actions.style.marginTop='12px';const edit=document.createElement('button');edit.textContent='Edit';edit.addEventListener('click',()=>openAccount(a));actions.append(edit);if(['oauth','native'].includes(a.auth_kind)&&['codex-cli','claude-code','custom-cli'].includes(a.preset)){{const login=document.createElement('button');login.textContent='Login';login.addEventListener('click',()=>loginAccount(a.id));actions.append(login);const st=document.createElement('button');st.textContent='Status';st.addEventListener('click',()=>statusAccount(a.id));actions.append(st)}}if(a.auth_kind==='api_key'&&state.keyring_available){{const key=document.createElement('button');key.textContent='Store key';key.addEventListener('click',()=>openSecret(a.id));actions.append(key)}}const del=document.createElement('button');del.className='danger';del.textContent='Remove';del.addEventListener('click',()=>deleteAccount(a.id));actions.append(del);card.append(actions);root.append(card)}})}}
function renderRoles(roles){{const root=$('roles');root.replaceChildren();if(!roles.length){{const p=document.createElement('p');p.className='muted';p.textContent='No GUI-managed role bindings yet.';root.append(p);return}}roles.forEach(r=>{{const card=document.createElement('article');card.className='card';const t=document.createElement('strong');t.textContent=r.id;card.append(t);const p=document.createElement('p');p.textContent=r.objective;card.append(p);const m=document.createElement('div');m.append(badge(r.mode),badge(r.executors.join(', ')));card.append(m);const actions=document.createElement('div');actions.className='row';actions.style.marginTop='12px';const edit=document.createElement('button');edit.textContent='Edit';edit.addEventListener('click',()=>openRole(r));actions.append(edit);const del=document.createElement('button');del.className='danger';del.textContent='Remove';del.addEventListener('click',()=>deleteRole(r.id));actions.append(del);card.append(actions);root.append(card)}})}}
function presetDefaults(preset){{if(preset==='codex-cli')return{{provider:'openai',auth_kind:'oauth',guard:'standard',max:'confidential'}};if(preset==='claude-code')return{{provider:'anthropic',auth_kind:'oauth',guard:'standard',max:'confidential'}};if(preset==='anthropic-api')return{{provider:'anthropic',auth_kind:'api_key',guard:'strict',max:'internal'}};if(preset==='openai-compatible-api')return{{provider:'',auth_kind:'api_key',guard:'strict',max:'internal'}};return{{provider:'',auth_kind:'oauth',guard:'standard',max:'confidential'}}}}
function updatePresetUi(applyDefaults=false){{const p=$('preset').value,d=presetDefaults(p);if(applyDefaults){{$('provider').value=d.provider;$('authKind').value=d.auth_kind;$('guardMode').value=d.guard;$('maxClass').value=d.max}}const api=['openai-compatible-api','anthropic-api'].includes(p);$('endpoint').disabled=!api;$('secretRef').disabled=$('authKind').value!=='api_key';$('customCli').style.display=p==='custom-cli'?'block':'none'}}
function openAccount(a=null){{$('preset').replaceChildren(...state.presets.map(p=>{{const o=document.createElement('option');o.value=p.id;o.textContent=p.label;return o}}));const x=a||{{id:'',account_label:'',preset:'codex-cli',provider:'openai',auth_kind:'oauth',email:'',model:'default',endpoint:'',secret_ref:'',roles:[],role_mode:'preferred',bound_roles_only:true,priority:100,budget:{{subscription_remaining_percent:100,max_parallel:1}},data_guard:{{mode:'standard',max_data_class:'confidential'}},command:[],login_command:[],auth_status_command:[],prompt_transport:'argument',sandbox_enforced:false}};$('accountTitle').textContent=a?'Edit agent account':'Add agent account';$('accountId').disabled=!!a;$('accountId').value=x.id;$('accountLabel').value=x.account_label;$('preset').value=x.preset;$('provider').value=x.provider;$('authKind').value=x.auth_kind;$('email').value=x.email||'';$('model').value=x.model||'default';$('endpoint').value=x.endpoint||'';$('secretRef').value=x.secret_ref||'';$('rolesInput').value=(x.roles||[]).join(', ');$('roleMode').value=x.role_mode||'preferred';$('boundOnly').checked=!!x.bound_roles_only;$('priority').value=x.priority??100;$('remaining').value=x.budget?.subscription_remaining_percent??100;$('parallel').value=x.budget?.max_parallel??1;$('guardMode').value=x.data_guard?.mode||'standard';$('maxClass').value=x.data_guard?.max_data_class||'confidential';$('command').value=(x.command||[]).join(' ');$('loginCommand').value=(x.login_command||[]).join(' ');$('statusCommand').value=(x.auth_status_command||[]).join(' ');$('promptTransport').value=x.prompt_transport||'argument';$('sandboxEnforced').checked=!!x.sandbox_enforced;updatePresetUi(false);$('accountDialog').showModal()}}
async function saveAccount(){{const accountId=$('accountId').value.trim();let secretRef=$('secretRef').value.trim()||null;if($('authKind').value==='api_key'&&!secretRef&&state.keyring_available)secretRef=`keyring:hermes-legion-commander/${{accountId}}`;const body={{id:accountId,account_label:$('accountLabel').value,preset:$('preset').value,provider:$('provider').value,auth_kind:$('authKind').value,email:$('email').value,model:$('model').value,endpoint:$('endpoint').value,secret_ref:secretRef,roles:splitCsv($('rolesInput').value),role_mode:$('roleMode').value,bound_roles_only:$('boundOnly').checked,priority:Number($('priority').value||100),budget:{{subscription_remaining_percent:Number($('remaining').value||100),max_parallel:Number($('parallel').value||1)}},data_guard:{{mode:$('guardMode').value,max_data_class:$('maxClass').value}},command:$('command').value,login_command:$('loginCommand').value,auth_status_command:$('statusCommand').value,prompt_transport:$('promptTransport').value,sandbox_enforced:$('sandboxEnforced').checked}};try{{await api('/api/accounts',{{method:'POST',body:JSON.stringify(body)}});$('accountDialog').close();setStatus('Account saved.');await refresh()}}catch(e){{setStatus(e.message,true)}}}}
function openRole(r=null){{$('roleId').disabled=!!r;$('roleId').value=r?.id||'';$('roleObjective').value=r?.objective||'';$('roleBindingMode').value=r?.mode||'allowed';$('roleExecutors').value=(r?.executors||[]).join(', ');$('roleDialog').showModal()}}
async function saveRole(){{try{{await api('/api/roles',{{method:'POST',body:JSON.stringify({{id:$('roleId').value,objective:$('roleObjective').value,mode:$('roleBindingMode').value,executors:splitCsv($('roleExecutors').value)}})}});$('roleDialog').close();setStatus('Role binding saved.');await refresh()}}catch(e){{setStatus(e.message,true)}}}}
async function deleteAccount(id){{if(!confirm(`Remove ${{id}} from the managed registry? Native provider credentials are not deleted.`))return;try{{await api(`/api/accounts/${{encodeURIComponent(id)}}`,{{method:'DELETE'}});await refresh()}}catch(e){{setStatus(e.message,true)}}}}async function deleteRole(id){{if(!confirm(`Remove role binding ${{id}}?`))return;try{{await api(`/api/roles/${{encodeURIComponent(id)}}`,{{method:'DELETE'}});await refresh()}}catch(e){{setStatus(e.message,true)}}}}
async function loginAccount(id){{try{{const r=await api(`/api/accounts/${{encodeURIComponent(id)}}/login`,{{method:'POST',body:'{{}}'}});setStatus(r.message||'Login started.')}}catch(e){{setStatus(e.message,true)}}}}async function statusAccount(id){{try{{const r=await api(`/api/accounts/${{encodeURIComponent(id)}}/status`,{{method:'POST',body:'{{}}'}});setStatus(`${{id}}: ${{r.ok?'authenticated':'not authenticated'}}`);alert(r.output||JSON.stringify(r,null,2))}}catch(e){{setStatus(e.message,true)}}}}
function openSecret(id){{$('secretAccountId').value=id;$('secretValue').value='';$('secretDialog').showModal()}}async function saveSecret(){{try{{await api(`/api/accounts/${{encodeURIComponent($('secretAccountId').value)}}/keyring`,{{method:'POST',body:JSON.stringify({{secret:$('secretValue').value}})}});$('secretValue').value='';$('secretDialog').close();setStatus('API key stored in OS keyring; only its reference is persisted.');await refresh()}}catch(e){{setStatus(e.message,true)}}}}
$('addAccount').addEventListener('click',()=>openAccount());$('addRole').addEventListener('click',()=>openRole());$('validate').addEventListener('click',async()=>{{try{{const r=await api('/api/validate',{{method:'POST',body:'{{}}'}});setStatus(`Valid: ${{r.executors}} executors, ${{r.roles}} roles, ${{r.runtimes}} runtimes.`)}}catch(e){{setStatus(e.message,true)}}}});$('saveAccount').addEventListener('click',saveAccount);$('cancelAccount').addEventListener('click',()=>$('accountDialog').close());$('saveRole').addEventListener('click',saveRole);$('cancelRole').addEventListener('click',()=>$('roleDialog').close());$('saveSecret').addEventListener('click',saveSecret);$('cancelSecret').addEventListener('click',()=>$('secretDialog').close());$('preset').addEventListener('change',()=>updatePresetUi(true));$('authKind').addEventListener('change',()=>updatePresetUi(false));refresh().catch(e=>setStatus(e.message,true));
</script>
</body></html>"""


def make_handler(state: PanelState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesLegionControlPanel/1"

        def log_message(self, format: str, *args: Any) -> None:
            super().log_message(format, *args)

        def _security_headers(self, content_type: str = "application/json; charset=utf-8") -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

        def _host_ok(self) -> bool:
            host_header = self.headers.get("Host", "")
            host = host_header.rsplit(":", 1)[0].casefold()
            return host in LOCAL_HOSTS

        def _json(self, status: int, payload: Mapping[str, Any]) -> None:
            data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or length > MAX_BODY:
                raise AccountRegistryError("request body exceeds local control-panel limit")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AccountRegistryError("invalid JSON request") from exc
            if not isinstance(value, dict):
                raise AccountRegistryError("JSON request root must be an object")
            return value

        def _write_authorized(self) -> bool:
            origin = self.headers.get("Origin")
            origin_ok = origin is None or origin.startswith(("http://127.0.0.1:", "http://localhost:"))
            return self._host_ok() and origin_ok and self.headers.get("X-HLC-CSRF", "") == state.csrf

        def do_GET(self) -> None:
            if not self._host_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "invalid Host header"})
                return
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = _html(state.csrf).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self._security_headers("text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/state":
                self._json(HTTPStatus.OK, state.state())
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._write_authorized():
                self._json(HTTPStatus.FORBIDDEN, {"error": "local CSRF/Host validation failed"})
                return
            parsed = urlparse(self.path)
            try:
                body = self._read_json()
                if parsed.path == "/api/accounts":
                    self._json(HTTPStatus.OK, {"ok": True, "registry": state.upsert_account(body)})
                    return
                if parsed.path == "/api/roles":
                    self._json(HTTPStatus.OK, {"ok": True, "registry": state.upsert_role(body)})
                    return
                if parsed.path == "/api/validate":
                    self._json(HTTPStatus.OK, state.validate())
                    return
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) == 4 and parts[:2] == ["api", "accounts"]:
                    account_id, action = parts[2], parts[3]
                    if action == "login":
                        self._json(HTTPStatus.OK, state.login(account_id)); return
                    if action == "status":
                        self._json(HTTPStatus.OK, state.status(account_id)); return
                    if action == "keyring":
                        self._json(HTTPStatus.OK, state.set_keyring_secret(account_id, str(body.get("secret") or ""))); return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_DELETE(self) -> None:
            if not self._write_authorized():
                self._json(HTTPStatus.FORBIDDEN, {"error": "local CSRF/Host validation failed"})
                return
            parsed = urlparse(self.path)
            try:
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) == 3 and parts[:2] == ["api", "accounts"]:
                    self._json(HTTPStatus.OK, {"ok": True, "registry": state.delete_account(parts[2])}); return
                if len(parts) == 3 and parts[:2] == ["api", "roles"]:
                    self._json(HTTPStatus.OK, {"ok": True, "registry": state.delete_role(parts[2])}); return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    return Handler


def serve(config_path: Path, *, port: int = 8765, open_browser: bool = True) -> int:
    state = PanelState(config_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Hermes Legion Commander control panel: {url}")
    print(f"Base config: {state.config_path}")
    print(f"Managed registry: {state.registry_path}")
    print("Bound to loopback only. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-legion-commander gui")
    parser.add_argument("--config", type=Path, default=Path("config/legion.toml"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not (0 <= args.port <= 65535):
        parser.error("--port must be between 0 and 65535")
    return serve(args.config, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(cli_main())
