"""Guest 加入、關係聲明、主人拍板、白名單的應用層。"""

from __future__ import annotations

from agent.brain.domain.guest import RelationDeclaration
from agent.brain.domain.principal import Principal


def join(state, *, invite_code: str, discord_id: str, display_name: str,
         declared_kind: str = "other", declared_label: str = "",
         declared_note: str = "", guest_relations=()) -> dict:
    request = state.app.guest_store.create_request(
        invite_code=invite_code, discord_id=discord_id, display_name=display_name,
        declared=RelationDeclaration(kind=declared_kind, label=declared_label,
                                     note=declared_note),
        guest_relations=guest_relations)
    return {"ok": True, "request_id": request.id,
            "request_secret": request.request_secret,
            "status": request.status.value,
            "declared": request.declared.to_dict()}


def status(state, secret: str) -> dict:
    request = state.app.guest_store.by_secret(secret)
    if request is None:
        return {"ok": False, "error": "unknown request"}
    view = request.to_dict()
    view.pop("token_sha256", None)
    # 明文 token 只交這一次：交出去就從 guests.json 清掉，之後只剩雜湊（2026-10-31）
    if request.token:
        state.app.guest_store.deliver_token(request)
    return {"ok": True, "request": view}


def directory(state) -> dict:
    return {"ok": True, "guests": state.app.guest_store.directory()}


def owner_guests(state) -> dict:
    return {"ok": True, "guests": state.app.guest_store.snapshot(),
            "whitelist": state.app.guest_store.whitelist()}


def approve(state, request_id: str, *, final_kind: str = "", final_label: str = "",
            trust: float = 0.5, capabilities=("chat",),
            final_guest_relations=()) -> dict:
    request = state.app.guest_store.approve(
        request_id,
        final=RelationDeclaration(kind=final_kind or "other",
                                  label=final_label or "访客"),
        trust=float(trust), capabilities=capabilities,
        final_guests=final_guest_relations)
    state.app.principals.register(
        Principal(request.principal_id, request.display_name, role="guest",
                  discord_id=request.discord_id), replace=True)
    if request.final is not None:
        state.app.social.add_relation(
            "owner", request.principal_id, request.final.kind,
            label=request.final.label, trust=request.trust)
    for relation in request.final_guests:
        if not relation.other_principal or relation.kind in {"stranger", "none"}:
            continue
        state.app.social.add_relation(
            request.principal_id, relation.other_principal, relation.kind,
            label=relation.label, trust=0.5)
        state.app.guest_store.add_pair(
            request.principal_id, relation.other_principal,
            kind=relation.kind, label=relation.label, note=relation.note)
    kind = request.final.kind if request.final is not None else "other"
    for entity in state.app.entities.all():
        state.app.stances.ensure_relation(entity.id, request.principal_id, kind)
    return {"ok": True, "request": request.to_dict(), "state": state.snapshot()}


def reject(state, request_id: str) -> dict:
    request = state.app.guest_store.reject(request_id)
    return {"ok": True, "request": request.to_dict(), "state": state.snapshot()}


def revoke(state, request_id: str) -> dict:
    request = state.app.guest_store.get(request_id)
    if request is None:
        return {"ok": False, "error": "unknown request"}
    ok = state.app.guest_store.revoke(request.principal_id)
    return {"ok": ok, "state": state.snapshot()}
