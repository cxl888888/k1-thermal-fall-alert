"""Shared family number and unanimous approval by the other paired phones."""
from sqlalchemy import select

from .db import Client, ContactPhoneChange, Credential


def current_phone(session, default):
    return session.scalar(select(ContactPhoneChange.phone)
                          .where(ContactPhoneChange.applied_at.is_not(None))
                          .order_by(ContactPhoneChange.id.desc()).limit(1)) or default


def pending_change(session):
    return session.scalar(select(ContactPhoneChange).join(Client)
                          .where(ContactPhoneChange.applied_at.is_(None),
                                 ContactPhoneChange.cancelled_at.is_(None),
                                 Client.revoked.is_(False))
                          .order_by(ContactPhoneChange.id.desc()).limit(1))


def other_clients(session, requester_id):
    paired = select(Credential.client_id).where(Credential.role == 'family')
    return set(session.scalars(select(Client.id).where(
        Client.revoked.is_(False), Client.id != requester_id, Client.id.in_(paired))))


def contact_view(session, client_id, default):
    change = pending_change(session)
    pending = None
    if change:
        requester = session.get(Client, change.requester_id)
        required = other_clients(session, change.requester_id)
        approved = required.intersection(change.approvals)
        mine = change.requester_id == client_id
        pending = dict(id=change.id, phone=change.phone, requester=requester.label, mine=mine,
                       required=len(required), approved=len(approved), i_approved=client_id in approved,
                       can_approve=client_id in required and client_id not in approved,
                       can_apply=mine and bool(required) and approved == required)
    return dict(phone=current_phone(session, default), pending=pending,
                can_propose=change is None and bool(other_clients(session, client_id)))
