import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional
from azure.cosmos import CosmosClient, exceptions
from app.config import settings
from app.models.case import CaseStatus, TERMINAL_STATES
from app.core.state_machine import transition
from app.services.local_store import LocalDocumentStore, LocalNotFoundError

NOT_FOUND = (exceptions.CosmosResourceNotFoundError, LocalNotFoundError)

logger = logging.getLogger(__name__)


class CosmosService:
    """
    Data layer. Uses Azure Cosmos DB when COSMOS_CONNECTION_STRING is set,
    otherwise a local SQLite document store with the same behaviour.
    """

    def __init__(self, local_path: Optional[str] = None):
        self.local = local_path is not None or not settings.use_cosmos
        if self.local:
            path = local_path or os.path.join(settings.LOCAL_DATA_DIR, "legalai.db")
            store = LocalDocumentStore(path)
            self.store        = store
            self.db           = None
            self.cases        = store.container("cases")
            self.users        = store.container("users")
            self.negotiations = store.container("negotiations")
            self.documents    = store.container("documents")
        else:
            self.client = CosmosClient.from_connection_string(
                settings.COSMOS_CONNECTION_STRING
            )
            self.db           = self.client.get_database_client(settings.COSMOS_DATABASE_NAME)
            self.cases        = self.db.get_container_client("cases")
            self.users        = self.db.get_container_client("users")
            self.negotiations = self.db.get_container_client("negotiations")
            self.documents    = self.db.get_container_client("documents")

    def ping(self) -> str:
        if self.local:
            self.store.ping()
            return "sqlite"
        self.db.read()
        return "cosmos"

    # ── Helpers ───────────────────────────────────────────────

    def _new_id(self) -> str:
        return str(uuid.uuid4())

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ── Cases ─────────────────────────────────────────────────

    def create_case(self, data: dict) -> dict:
        case = {
            "id":         self._new_id(),
            "created_at": self._now(),
            "updated_at": self._now(),
            "status":     "SUBMITTED",
            **data,
        }
        self.cases.create_item(body=case)
        logger.info(f"Case created | id={case['id']}")
        return case

    def get_case(self, case_id: str) -> Optional[dict]:
        try:
            return self.cases.read_item(item=case_id, partition_key=case_id)
        except NOT_FOUND:
            return None

    def update_case(self, case_id: str, updates: dict) -> dict:
        case = self.get_case(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")
        case.update(updates)
        case["updated_at"] = self._now()
        self.cases.replace_item(item=case_id, body=case)
        logger.info(f"Case updated | id={case_id}")
        return case

    def transition_case(self, case_id: str, new_status: CaseStatus,
                        extra_updates: dict = None) -> dict:
        """
        THE ONLY WAY to change case status in the entire app.
        Validates the transition, sets deadline, saves to Cosmos.
        """
        case = self.get_case(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")
        updated = transition(case, new_status)
        if extra_updates:
            updated.update(extra_updates)
        self.cases.replace_item(item=case_id, body=updated)
        return updated

    def get_cases_by_email(self, email: str) -> list:
        if self.local:
            e = email.lower()
            return self.cases.find(
                lambda c: (c.get("claimant_email") or "").lower() == e
                or (c.get("respondent_email") or "").lower() == e,
                sort_key="created_at", descending=True,
            )
        q = ("SELECT * FROM c "
             "WHERE c.claimant_email=@e OR c.respondent_email=@e "
             "ORDER BY c.created_at DESC")
        return list(self.cases.query_items(
            query=q,
            parameters=[{"name": "@e", "value": email}],
            enable_cross_partition_query=True,
        ))

    def get_cases_by_status(self, status: CaseStatus) -> list:
        if self.local:
            return self.cases.find(lambda c: c.get("status") == status.value)
        q = "SELECT * FROM c WHERE c.status = @s"
        return list(self.cases.query_items(
            query=q,
            parameters=[{"name": "@s", "value": status.value}],
            enable_cross_partition_query=True,
        ))

    def get_cases_needing_expiry_check(self) -> list:
        """All non-terminal cases that have a deadline set."""
        if self.local:
            done = {"SETTLED", "ESCALATED", "AUTO_ESCALATED", "ABANDONED", "CRIMINAL_ADVISORY"}
            return self.cases.find(
                lambda c: "current_deadline" in c and c.get("status") not in done
            )
        q = ("SELECT * FROM c "
             "WHERE IS_DEFINED(c.current_deadline) "
             "AND c.status NOT IN ('SETTLED','ESCALATED',"
             "'AUTO_ESCALATED','ABANDONED','CRIMINAL_ADVISORY')")
        return list(self.cases.query_items(
            query=q,
            enable_cross_partition_query=True,
        ))

    def save_agent_output(self, case_id: str, agent_name: str, output: dict) -> dict:
        """Saves a specific agent's output. agent_name must be one of the 4 valid names."""
        valid = ['intake_data', 'legal_data', 'analytics_data', 'documents_data']
        if agent_name not in valid:
            raise ValueError(f"Invalid agent name: {agent_name}")
        return self.update_case(case_id, {agent_name: output})

    def get_case_for_respondent(self, case_id: str) -> Optional[dict]:
        """
        Filtered view of case for the respondent.
        Removes analytics_data (contains win probability + ZOPA)
        and evidence strength score.
        """
        case = self.get_case(case_id)
        if not case:
            return None
        hidden = ["analytics_data", "claimant_phone", "disclaimer_acknowledged"]
        view = {k: v for k, v in case.items() if k not in hidden}
        if view.get("intake_data"):
            ic = dict(view["intake_data"])
            ic.pop("evidence_strength_score", None)
            view["intake_data"] = ic
        from app.services.blob_service import refresh_case_urls
        return refresh_case_urls(view)

    def set_respondent_verified(self, case_id: str) -> dict:
        return self.update_case(case_id, {"respondent_verified": True})

    def set_settlement_honored(self, case_id: str, honored: bool) -> dict:
        return self.update_case(case_id, {"settlement_honored": honored})

    def append_case_reasoning_log(
        self,
        case_id: str,
        stage: str,
        title: str,
        detail: str,
        metadata: Optional[dict] = None,
    ) -> dict:
        case = self.get_case(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")
        logs = case.get("ai_reasoning_log", [])
        logs.append({
            "id": self._new_id(),
            "timestamp": self._now(),
            "stage": stage,
            "title": title,
            "detail": detail,
            "metadata": metadata or {},
        })
        return self.update_case(case_id, {"ai_reasoning_log": logs})

    # ── Users ─────────────────────────────────────────────────

    def get_user_by_email(self, email: str) -> Optional[dict]:
        if self.local:
            # OTP documents share the users container but have an "otp" field.
            r = self.users.find(lambda u: u.get("email") == email and "otp" not in u)
            return r[0] if r else None
        q = "SELECT * FROM c WHERE c.email = @e AND NOT IS_DEFINED(c.otp)"
        r = list(self.users.query_items(
            query=q,
            parameters=[{"name": "@e", "value": email}],
            enable_cross_partition_query=True,
        ))
        return r[0] if r else None

    def upsert_user(self, data: dict) -> dict:
        existing = self.get_user_by_email(data.get("email", ""))
        if existing:
            existing.update(data)
            existing["updated_at"] = self._now()
            self.users.replace_item(item=existing["id"], body=existing)
            return existing
        user = {"id": self._new_id(), "created_at": self._now(), **data}
        self.users.create_item(body=user)
        return user

    def save_otp(self, email: str, otp: str, expires_at: str):
        """Store OTP. Uses deterministic ID so upsert overwrites previous OTP."""
        otp_id = f"otp_{email.replace('@','_').replace('.','_')}"
        doc = {
            "id":         otp_id,
            "email":      email,
            "otp":        otp,
            "expires_at": expires_at,
            "created_at": self._now(),
        }
        self.users.upsert_item(body=doc)

    def get_otp(self, email: str) -> Optional[dict]:
        otp_id = f"otp_{email.replace('@','_').replace('.','_')}"
        try:
            return self.users.read_item(item=otp_id, partition_key=otp_id)
        except NOT_FOUND:
            return None

    def delete_otp(self, email: str):
        otp_id = f"otp_{email.replace('@','_').replace('.','_')}"
        try:
            self.users.delete_item(item=otp_id, partition_key=otp_id)
        except Exception:
            pass

    # ── Negotiations ──────────────────────────────────────────

    def create_negotiation(self, case_id: str, data: dict) -> dict:
        neg = {
            "id":         self._new_id(),
            "case_id":    case_id,
            "created_at": self._now(),
            "rounds":     [],
            **data,
        }
        self.negotiations.create_item(body=neg)
        return neg

    def get_negotiation_by_case(self, case_id: str) -> Optional[dict]:
        if self.local:
            r = self.negotiations.find(lambda n: n.get("case_id") == case_id)
            return r[0] if r else None
        q = "SELECT * FROM c WHERE c.case_id = @cid"
        r = list(self.negotiations.query_items(
            query=q,
            parameters=[{"name": "@cid", "value": case_id}],
            enable_cross_partition_query=True,
        ))
        return r[0] if r else None

    def update_negotiation(self, neg_id: str, updates: dict) -> dict:
        neg = self.negotiations.read_item(item=neg_id, partition_key=neg_id)
        neg.update(updates)
        neg["updated_at"] = self._now()
        self.negotiations.replace_item(item=neg_id, body=neg)
        return neg

    def add_round_to_negotiation(self, neg_id: str, round_data: dict) -> dict:
        neg = self.negotiations.read_item(item=neg_id, partition_key=neg_id)
        neg["rounds"].append(round_data)
        neg["updated_at"] = self._now()
        self.negotiations.replace_item(item=neg_id, body=neg)
        return neg

    def update_round_in_negotiation(self, neg_id: str, round_number: int,
                                    updates: dict) -> dict:
        neg = self.negotiations.read_item(item=neg_id, partition_key=neg_id)
        for i, r in enumerate(neg["rounds"]):
            if r["round_number"] == round_number:
                neg["rounds"][i].update(updates)
                break
        neg["updated_at"] = self._now()
        self.negotiations.replace_item(item=neg_id, body=neg)
        return neg

    def upsert_round_in_negotiation(self, neg_id: str, round_number: int, round_data: dict) -> dict:
        neg = self.negotiations.read_item(item=neg_id, partition_key=neg_id)
        replaced = False
        for i, existing_round in enumerate(neg["rounds"]):
            if existing_round["round_number"] == round_number:
                neg["rounds"][i] = round_data
                replaced = True
                break
        if not replaced:
            neg["rounds"].append(round_data)
        neg["updated_at"] = self._now()
        self.negotiations.replace_item(item=neg_id, body=neg)
        return neg

    def get_round(self, neg_id: str, round_number: int) -> Optional[dict]:
        neg = self.negotiations.read_item(item=neg_id, partition_key=neg_id)
        for r in neg["rounds"]:
            if r["round_number"] == round_number:
                return r
        return None

    def append_proof_request(self, neg_id: str, proof_request: dict) -> dict:
        neg = self.negotiations.read_item(item=neg_id, partition_key=neg_id)
        neg.setdefault("proof_requests", []).append(proof_request)
        neg["updated_at"] = self._now()
        self.negotiations.replace_item(item=neg_id, body=neg)
        return neg

    def update_proof_request(self, neg_id: str, request_id: str, updates: dict) -> dict:
        neg = self.negotiations.read_item(item=neg_id, partition_key=neg_id)
        for item in neg.get("proof_requests", []):
            if item["id"] == request_id:
                item.update(updates)
                break
        neg["updated_at"] = self._now()
        self.negotiations.replace_item(item=neg_id, body=neg)
        return neg

    def append_shared_note(self, neg_id: str, note: dict) -> dict:
        neg = self.negotiations.read_item(item=neg_id, partition_key=neg_id)
        neg.setdefault("shared_notes", []).append(note)
        neg["updated_at"] = self._now()
        self.negotiations.replace_item(item=neg_id, body=neg)
        return neg

    # ── Documents ─────────────────────────────────────────────

    def save_document_record(self, case_id: str, doc_type: str, blob_url: str) -> dict:
        doc = {
            "id":         self._new_id(),
            "case_id":    case_id,
            "doc_type":   doc_type,
            "blob_url":   blob_url,
            "created_at": self._now(),
        }
        self.documents.create_item(body=doc)
        return doc

    def get_documents_by_case(self, case_id: str) -> list:
        if self.local:
            return self.documents.find(
                lambda d: d.get("case_id") == case_id and "doc_type" in d,
                sort_key="created_at", descending=True,
            )
        q = ("SELECT * FROM c WHERE c.case_id=@cid AND IS_DEFINED(c.doc_type) "
             "ORDER BY c.created_at DESC")
        return list(self.documents.query_items(
            query=q,
            parameters=[{"name": "@cid", "value": case_id}],
            enable_cross_partition_query=True,
        ))
    def log_email_sent(
        self,
        case_id: str,
        to_email: str,
        email_type: str,
        subject: str,
    ) -> None:
        """
        Logs every email sent for a case.
        Useful for debugging and audit trail.
        """
        try:
            log_doc = {
                "id":         f"email_{self._new_id()}",
                "case_id":    case_id,
                "to_email":   to_email,
                "email_type": email_type,
                "subject":    subject,
                "sent_at":    self._now(),
            }
            self.documents.create_item(body=log_doc)
        except Exception as e:
            logger.warning(f"Email log failed (non-critical) | error={e}")

# Singleton — import this everywhere
cosmos_service = CosmosService()
