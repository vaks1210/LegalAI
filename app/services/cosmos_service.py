import os
import random
import time
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from azure.core import MatchConditions
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
            self.organizations = store.container("organizations")
        else:
            self.client = CosmosClient.from_connection_string(
                settings.COSMOS_CONNECTION_STRING
            )
            self.db           = self.client.get_database_client(settings.COSMOS_DATABASE_NAME)
            self.cases        = self.db.get_container_client("cases")
            self.users        = self.db.get_container_client("users")
            self.negotiations = self.db.get_container_client("negotiations")
            self.documents    = self.db.get_container_client("documents")
            self.organizations = self.db.get_container_client("organizations")

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

    def _mutate(self, container, item_id: str, fn, retries: int = 8) -> dict:
        """
        Atomic read-modify-write. `fn(doc)` mutates the document in place.

        Concurrent writers (two parties submitting at once, background
        mediation vs. a user request) would otherwise silently overwrite
        each other. Locally the store lock is held across the read and
        write; on Cosmos the write is conditional on the ETag and retried.
        """
        if self.local:
            with self.store.lock:
                doc = container.read_item(item=item_id, partition_key=item_id)
                fn(doc)
                container.replace_item(item=item_id, body=doc)
                return doc
        for attempt in range(retries):
            doc = container.read_item(item=item_id, partition_key=item_id)
            fn(doc)
            try:
                return container.replace_item(
                    item=item_id, body=doc,
                    etag=doc.get("_etag"), match_condition=MatchConditions.IfNotModified,
                )
            except exceptions.CosmosAccessConditionFailedError:
                logger.info(f"ETag conflict, retrying | id={item_id} | attempt={attempt + 1}")
                time.sleep(random.uniform(0, 0.02 * (attempt + 1)))  # jitter so writers desynchronise
        raise RuntimeError(f"Could not update {item_id}: too many concurrent writes")

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

    def _mutate_case(self, case_id: str, fn) -> dict:
        try:
            return self._mutate(self.cases, case_id, fn)
        except NOT_FOUND:
            raise ValueError(f"Case {case_id} not found")

    def update_case(self, case_id: str, updates: dict) -> dict:
        def apply(case):
            case.update(updates)
            case["updated_at"] = self._now()
        case = self._mutate_case(case_id, apply)
        logger.info(f"Case updated | id={case_id}")
        return case

    def transition_case(self, case_id: str, new_status: CaseStatus,
                        extra_updates: dict = None) -> dict:
        """
        THE ONLY WAY to change case status in the entire app.
        Validates the transition against the *current* stored status
        atomically, sets the deadline and saves.
        """
        previous = {}

        def apply(case):
            previous["status"] = case.get("status")
            transition(case, new_status)
            if extra_updates:
                case.update(extra_updates)
        updated = self._mutate_case(case_id, apply)
        if updated.get("org_id"):
            from app.services.webhooks import on_case_transition
            on_case_transition(updated, previous.get("status"))
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

    # What the respondent may see. An allow-list: the previous deny-list hid
    # only analytics_data, so the claimant's private strategy (incl. their
    # settlement floor), analysed weaknesses and the court file (which embeds
    # analytics) all reached the opposing party.
    RESPONDENT_CASE_FIELDS = (
        "id", "status", "track", "created_at", "updated_at", "current_deadline",
        "current_round", "max_rounds", "action_required_by",
        "claimant_name", "claimant_city", "claimant_state",
        "respondent_name", "respondent_email", "respondent_type", "respondent_company_name",
        "claim_amount", "currency", "dispute_text", "incident_date", "evidence_files",
        "respondent_version", "respondent_counter_amount",
        "settlement_url", "settled_amount", "settled_at", "payment", "settlement_sha256",
    )
    RESPONDENT_INTAKE_FIELDS = ("dispute_type", "severity", "key_issues", "key_facts")
    RESPONDENT_LEGAL_FIELDS = (
        "applicable_laws", "forum_name", "respondent_defenses", "relief_available",
        "legal_notice_required",
    )
    RESPONDENT_DOCUMENT_FIELDS = ("demand_letter_url", "settlement_url", "mediation_certificate_url")

    def get_case_for_respondent(self, case_id: str) -> Optional[dict]:
        """Filtered (allow-list) view of a case for the respondent."""
        case = self.get_case(case_id)
        if not case:
            return None

        def pick(src, keys):
            src = src or {}
            return {k: src[k] for k in keys if k in src}

        view = pick(case, self.RESPONDENT_CASE_FIELDS)
        view["intake_data"]    = pick(case.get("intake_data"), self.RESPONDENT_INTAKE_FIELDS)
        view["legal_data"]     = pick(case.get("legal_data"), self.RESPONDENT_LEGAL_FIELDS)
        view["documents_data"] = pick(case.get("documents_data"), self.RESPONDENT_DOCUMENT_FIELDS)
        from app.services.blob_service import refresh_case_urls
        return refresh_case_urls(view)

    def delete_case(self, case_id: str) -> None:
        """
        Hard-delete a case together with its negotiation(s) and every record
        in the documents container that points at it (document records and
        email logs). Stored files are the caller's job (see blob_service).
        """
        for neg in self._find_by_case(self.negotiations, case_id):
            self.negotiations.delete_item(item=neg["id"], partition_key=neg["id"])
        for doc in self._find_by_case(self.documents, case_id):
            self.documents.delete_item(item=doc["id"], partition_key=doc["id"])
        try:
            self.cases.delete_item(item=case_id, partition_key=case_id)
        except NOT_FOUND:
            pass
        logger.info(f"Case deleted | id={case_id}")

    def get_case_documents_all(self, case_id: str) -> list:
        """Every documents-container record for a case (incl. email logs)."""
        return self._find_by_case(self.documents, case_id)

    def _find_by_case(self, container, case_id: str) -> list:
        if self.local:
            return container.find(lambda d: d.get("case_id") == case_id)
        return list(container.query_items(
            query="SELECT * FROM c WHERE c.case_id = @cid",
            parameters=[{"name": "@cid", "value": case_id}],
            enable_cross_partition_query=True,
        ))

    ERASED_NAME = "[erased]"

    def minimise_claimant_on_case(self, case_id: str) -> dict:
        """
        Account erasure for a case that is retained (it reached the
        respondent): drop the claimant's phone and redact their name. The
        email stays — it links the record to the party and the respondent
        already holds it. City/state stay: they decide the forum.
        """
        def apply(case):
            case.pop("claimant_phone", None)
            case["claimant_name"] = self.ERASED_NAME
            case["claimant_erased_at"] = self._now()
            case["updated_at"] = self._now()
        return self._mutate_case(case_id, apply)

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
        entry = {
            "id": self._new_id(),
            "timestamp": self._now(),
            "stage": stage,
            "title": title,
            "detail": detail,
            "metadata": metadata or {},
        }
        def apply(case):
            case.setdefault("ai_reasoning_log", []).append(entry)
            case["updated_at"] = self._now()
        return self._mutate_case(case_id, apply)

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

    def allow_otp_request(self, email: str, max_requests: int = 5, window_seconds: int = 3600) -> bool:
        """
        Per-email OTP throttle (independent of IP, so rotating IPs doesn't
        help). Stored apart from the OTP itself so that deleting a code —
        on success or lockout — doesn't reset the history. No "email" field
        on purpose: user lookups match on it.
        """
        doc_id = f"otpreq_{email.replace('@','_').replace('.','_')}"
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
        allowed = {"ok": True}

        def apply(doc):
            recent = [t for t in doc.get("requests", []) if t > cutoff]
            if len(recent) >= max_requests:
                allowed["ok"] = False
            else:
                recent.append(now.isoformat())
            doc["requests"] = recent

        try:
            self._mutate(self.users, doc_id, apply)
        except NOT_FOUND:
            try:
                self.users.create_item(body={"id": doc_id, "doc_kind": "otp_requests",
                                             "requests": [now.isoformat()]})
            except Exception:
                # Created concurrently by another request — count this one too.
                self._mutate(self.users, doc_id, apply)
        return allowed["ok"]

    def delete_user_data(self, email: str) -> int:
        """
        Erase a user's profile and their OTP / OTP-throttle documents from
        the users container. Returns the number of documents removed.
        """
        key = email.replace('@', '_').replace('.', '_')
        ids = {f"otp_{key}", f"otpreq_{key}"}
        while True:  # normally one profile; loop in case of a past duplicate
            profile = self.get_user_by_email(email)
            if not profile or profile["id"] in ids:
                break
            ids.add(profile["id"])
            self.users.delete_item(item=profile["id"], partition_key=profile["id"])
        removed = len(ids) - 2
        for doc_id in (f"otp_{key}", f"otpreq_{key}"):
            try:
                self.users.read_item(item=doc_id, partition_key=doc_id)
            except NOT_FOUND:
                continue
            self.users.delete_item(item=doc_id, partition_key=doc_id)
            removed += 1
        logger.info(f"User data erased | docs={removed}")
        return removed

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

    def _mutate_negotiation(self, neg_id: str, fn) -> dict:
        def apply(neg):
            fn(neg)
            neg["updated_at"] = self._now()
        return self._mutate(self.negotiations, neg_id, apply)

    def update_negotiation(self, neg_id: str, updates: dict) -> dict:
        return self._mutate_negotiation(neg_id, lambda neg: neg.update(updates))

    def add_round_to_negotiation(self, neg_id: str, round_data: dict) -> dict:
        def apply(neg):
            if not any(r["round_number"] == round_data["round_number"] for r in neg["rounds"]):
                neg["rounds"].append(round_data)
        return self._mutate_negotiation(neg_id, apply)

    def mutate_round(self, neg_id: str, round_number: int, fn, default: dict = None) -> dict:
        """
        Atomically apply `fn(round)` to one round (creating it from `default`
        if missing) and return the round as stored. Callers change only the
        fields they own, so concurrent updates by the two parties both land.
        """
        result = {}

        def apply(neg):
            for r in neg["rounds"]:
                if r["round_number"] == round_number:
                    target = r
                    break
            else:
                if default is None:
                    raise ValueError(f"Round {round_number} not found")
                target = dict(default)
                neg["rounds"].append(target)
            fn(target)
            result["round"] = target

        self._mutate_negotiation(neg_id, apply)
        return result["round"]

    def update_round_in_negotiation(self, neg_id: str, round_number: int,
                                    updates: dict) -> dict:
        try:
            self.mutate_round(neg_id, round_number, lambda r: r.update(updates))
        except ValueError:
            pass
        return self.negotiations.read_item(item=neg_id, partition_key=neg_id)

    def upsert_round_in_negotiation(self, neg_id: str, round_number: int, round_data: dict) -> dict:
        def replace(r):
            r.clear()
            r.update(round_data)
        self.mutate_round(neg_id, round_number, replace, default=round_data)
        return self.negotiations.read_item(item=neg_id, partition_key=neg_id)

    def get_round(self, neg_id: str, round_number: int) -> Optional[dict]:
        neg = self.negotiations.read_item(item=neg_id, partition_key=neg_id)
        for r in neg["rounds"]:
            if r["round_number"] == round_number:
                return r
        return None

    def append_proof_request(self, neg_id: str, proof_request: dict) -> dict:
        return self._mutate_negotiation(
            neg_id, lambda neg: neg.setdefault("proof_requests", []).append(proof_request)
        )

    def update_proof_request(self, neg_id: str, request_id: str, updates: dict) -> dict:
        def apply(neg):
            for item in neg.get("proof_requests", []):
                if item["id"] == request_id:
                    item.update(updates)
                    break
        return self._mutate_negotiation(neg_id, apply)

    def append_shared_note(self, neg_id: str, note: dict) -> dict:
        return self._mutate_negotiation(
            neg_id, lambda neg: neg.setdefault("shared_notes", []).append(note)
        )

    def find_case_by_settlement_hash(self, sha256: str) -> Optional[dict]:
        if self.local:
            r = self.cases.find(lambda c: c.get("settlement_sha256") == sha256)
            return r[0] if r else None
        q = "SELECT * FROM c WHERE c.settlement_sha256 = @h"
        r = list(self.cases.query_items(query=q, parameters=[{"name": "@h", "value": sha256}],
                                        enable_cross_partition_query=True))
        return r[0] if r else None

    # ── Organisations (B2B tenants) ───────────────────────────

    def create_org(self, data: dict) -> dict:
        org = {"id": self._new_id(), "created_at": self._now(), **data}
        self.organizations.create_item(body=org)
        return org

    def get_org(self, org_id: str) -> Optional[dict]:
        try:
            return self.organizations.read_item(item=org_id, partition_key=org_id)
        except NOT_FOUND:
            return None

    def mutate_org(self, org_id: str, fn) -> dict:
        def apply(org):
            fn(org)
            org["updated_at"] = self._now()
        return self._mutate(self.organizations, org_id, apply)

    def get_orgs_for_member(self, email: str) -> list:
        email = email.lower()
        if self.local:
            return self.organizations.find(
                lambda o: any(m.get("email") == email for m in o.get("members", []))
            )
        q = ("SELECT * FROM c WHERE EXISTS("
             "SELECT VALUE m FROM m IN c.members WHERE m.email = @e)")
        return list(self.organizations.query_items(
            query=q, parameters=[{"name": "@e", "value": email}],
            enable_cross_partition_query=True,
        ))

    def get_org_by_api_key_hash(self, key_hash: str) -> Optional[dict]:
        if self.local:
            r = self.organizations.find(
                lambda o: any(k.get("hash") == key_hash and not k.get("revoked")
                              for k in o.get("api_keys", []))
            )
            return r[0] if r else None
        q = ("SELECT * FROM c WHERE EXISTS(SELECT VALUE k FROM k IN c.api_keys "
             "WHERE k.hash = @h AND NOT IS_DEFINED(k.revoked))")
        r = list(self.organizations.query_items(
            query=q, parameters=[{"name": "@h", "value": key_hash}],
            enable_cross_partition_query=True,
        ))
        return r[0] if r else None

    def get_cases_by_org(self, org_id: str) -> list:
        if self.local:
            return self.cases.find(lambda c: c.get("org_id") == org_id,
                                   sort_key="created_at", descending=True)
        q = "SELECT * FROM c WHERE c.org_id = @o ORDER BY c.created_at DESC"
        return list(self.cases.query_items(
            query=q, parameters=[{"name": "@o", "value": org_id}],
            enable_cross_partition_query=True,
        ))

    def find_org_case_by_reference(self, org_id: str, reference: str) -> Optional[dict]:
        if self.local:
            r = self.cases.find(lambda c: c.get("org_id") == org_id
                                and c.get("external_reference") == reference)
            return r[0] if r else None
        q = "SELECT * FROM c WHERE c.org_id = @o AND c.external_reference = @r"
        r = list(self.cases.query_items(
            query=q, parameters=[{"name": "@o", "value": org_id}, {"name": "@r", "value": reference}],
            enable_cross_partition_query=True,
        ))
        return r[0] if r else None

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
