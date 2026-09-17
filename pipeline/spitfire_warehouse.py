"""Every readable Spitfire endpoint, landed in typed tables in its own database.

`pipeline/spitfire_mirror.py` keeps 14 header columns and 18 line columns out of responses that
carry 116 and 64 fields, for the handful of purchase orders a reviewer happened to open. That is a
cache, and a deliberately narrow one. This is the warehouse: every PO in every configured project,
every endpoint that answers, one real column per API field.

Three decisions worth knowing before reading the schema.

**API field names are kept verbatim, in Spitfire's own PascalCase.** `DocMasterKey` stays
`DocMasterKey`; it is not renamed to `doc_master_key`. Renaming would mean maintaining a mapping
that nothing can check, and the fidelity test in `tests/test_spitfire_warehouse.py` — every key in
the archived response is a column holding an equal value — only works if the names match. Our own
bookkeeping columns are snake_case, so which is which is visible at a glance and the two can never
collide.

**The declared columns are what the live API actually returned**, sampled across eight purchase
orders on 2026-08-26 so a field that is null on one document is still caught on another. They are
not transcribed from the Swagger, which declares no enums and would not have settled affinity.
`_widen()` adds any field a later build introduces — the server has already moved 9692 -> 9727 ->
9728 during this project — because a silently dropped field is the failure
`spitfire_mirror._LINE_COLUMNS` exists to warn about.

**Nothing here is state.** Losing this file costs a re-sweep and nothing else; no pipeline decision
is recorded in it. That is why it is a separate database from `pipeline_state.sqlite3` rather than
fifteen more tables in it — see `config/settings.SPITFIRE_WAREHOUSE_DB_PATH`.

Writes here are strictly local. Nothing in this module talks to Spitfire; `connectors/spitfire.py`
does the reading, and it cannot write.
"""

import gzip
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config.settings import SPITFIRE_WAREHOUSE_DB_PATH

# --- what each endpoint returns -----------------------------------------------
# table -> sqlite affinity -> the API fields carrying it, whitespace separated. Grouping by
# affinity rather than listing 116 one-per-line pairs keeps the whole schema legible in one screen
# per table; `_api_columns()` flattens it back out.
_API_COLUMNS: Dict[str, Dict[str, str]] = {
    "sf_document": {
        "TEXT": """
            DocMasterKey DocTypeKey DocTypeKey_dv DocReference DocDate DocNo SourceDocNo
            ExternalDocNo DocBatchNo Title Source Due Closed Signoff SourceDate LinkedDocKey
            UniReferenceKey ResponsibleParty ResponsibleParty_dv SourceContact OwnerApprover
            LastStatusBy LastRouteKey Status Status_dv Subtype ContractType Reason Location
            PayItemNumber DivisionID Project Project_dv ProjEntity Specification BudgetRevFlag
            SOVItemNumber Section SubContract PayControl FromUser DocRevKey DocSessionKey
            ProjectSubtype TDKeyMapKey EditUser ComplianceInfo AttachmentInfo Description Segment
            Subsegment DraftNumber ArchProject TaxID TaxHandling InPeriod Notes NoteA NoteB NoteEML
            EmailSubject csDate csWhen csString016 csString030 csString040 csString050 csString060
            csString080 csString100 csString120 csString240 csNote csCode csContactKey csKey Created
            ETag
        """,
        "REAL": """
            CWRetention SMRetention CostImpact RangeFrom RangeThru Bond BondRate TaxRate csAmount
            csValue csQty sfVersion
        """,
        "INTEGER": """
            Priority NumToSend NumToForward Probability AutoTitled Confidential DocEdit Final
            DocFlag Area Duration UpdateMask RouteFlags MaxStage MaxRevNo CurrentSeq IsXTS
            XTSBlockIn XTSBlockOut RevNo DaysRequested DaysApproved csNumber csCheck csFlag HasBFASS
        """,
    },
    "sf_document_item": {
        "TEXT": """
            DocItemKey ResponsibleParty LinkedDocKey LinkedItemKey UniReferenceKey LastStatusBy
            Approver Author Description DrawingNumber RevisionNumber Paragraph Specification
            ItemStatus ItemSource ItemType ItemSubtype Drawings Samples ProductData TestReport
            MixDesign Schedule FieldMockup Guarantee Certification Evaluation Shop SourceItemNumber
            SourceInitialNumber ArchitectInitialNumber ArchitectItemNumber SOVLineNumber
            Manufacturer Supplier RevenueEntity Started Submitted Requested Received Reviewed Due
            Completed ItemRevKey ItemFolderKey ItemFromUser DocItemNumber ItemCreated
            ItemRegisterNumber ResponsibleNow ResponsibleCommon ETag
        """,
        "REAL": """
            CWRetention SMRetention OriginalEstimate OriginalQuote ItemQuantity
        """,
        "INTEGER": """
            TaskCount CommentCount LinkCount Stage Billable IsRegisterDoc ItemIsShared
        """,
    },
    "sf_item_task": {
        "TEXT": """
            ItemTaskKey LinkedLineKey LinkedRFQKey LinkedCCCKey ProjEntity ProjEntityDescription
            AccountCategory Subcontract SubChangeOrder CostType GLAcct GLSub LaborClass UOM
            RetentionMethod Vendor ProjectReference Note csNote csCode csString016 csString030
            csString040 csString050 csString060 csString080 csString100 csString120 csString240
            csContactKey csKey csDate csWhen Created LinkedContact LinkedRFQSC LinkedStatus ETag
        """,
        "REAL": """
            Quantity RevenueAmount Rate ExpenseAmount StoredAmount WorkAmount RetentionAmount
            SMRetentionAmount SOVWork SOVMaterials MarkupRate csAmount csValue csQty LinkedEstimate
            LinkedQuote LinkedExpense
        """,
        "INTEGER": """
            ItemPercent csNumber MarkupControl csCheck csFlag LinkedLines
        """,
    },
    "sf_item_related": {
        "TEXT": """
            Project Subcontract SCDocItemKey ProjEntity GLAcct GLSub LaborClass AccountCategory
            RetentionMethod LineDesc UOM ETag
        """,
        "REAL": """
            ApprovedQuantity ApprovedRetention ApprovedAmount ApprovedExpense PendingChangeUnits
            PendingChangeRetention PendingChangeAmount PendingChangeExpense VoucheredUnits
            VoucheredAmount VoucheredSMAmount VoucheredRetention VoucheredSMRetention
            PRInProgressQuantity PRInProgressRetention PRInProgressAmount ReceivedUnits
            ReceiptInProgressUnits ContractAmount ContractUnits TotalPercentRequest
            TotalUnitsCompleted TotalAmountCompleted TotalPriorAmount Rate
        """,
        "INTEGER": """
            ItemPercent Cap
        """,
    },
    "sf_item_revision_map": {
        "TEXT": """
            DocRevItemKey DocItemKey ContainerKey FromUser ItemNumber Created ETag
        """,
        "INTEGER": """
            ItemSeq
        """,
    },
    "sf_document_address": {
        "TEXT": """
            DocAddrKey AddrType SourceType UserKey Person Company Addr1 Addr2 City State Zip Phone
            Fax Email ContactProject RoleName Title ETag
        """,
        "INTEGER": """
            UseSource
        """,
    },
    "sf_document_route": {
        "TEXT": """
            RouteID UserKey FromUser RecipientRole Status RouteVia EmailFrom Note Request Response
            ResponseCode WorkflowScript Alerted Viewed Downloaded Due Acted UserName Activity
            RouteeProxy SuppressNotifyUntil RouteStepKey Reached ByUser Created TransStatus BinType
            ETag
        """,
        "INTEGER": """
            UserKey_Inactive Stage Sequence GroupNo UserDocEdit SendAlerts ReplyTo PriorityOver
            ExpectProxy HasContent IsInternal TransNumber HasBinData
        """,
    },
    "sf_document_date": {
        "TEXT": """
            DocDateRowKey DocDateTypeKey SchedStart SchedFinish ActStart ActFinish Note csNote
            csCode csString016 csString030 csString040 csString050 csString060 csString080
            csString100 csString120 csString240 csContactKey csKey csDate csWhen ETag
        """,
        "REAL": """
            csAmount csValue csQty
        """,
        "INTEGER": """
            Sequence IncludeStart IncludeFinish IsRequired LeadTime DaysBetween IsDone csNumber
            csCheck csFlag
        """,
    },
    "sf_document_attachment": {
        "TEXT": """
            DocAttachKey DocKey AttachedDocMaster LinkedItemKey AttachedItemNumber Note FromUser
            FromRouteID Created AccessLevel MailRoute DocReference CatType Status ResponsibleName
            RelationshipType ContainerKey FileName keyword Other SourceDocNo SourceBatchNo
            ReferenceDate FileType Project DivisionID SourceContact SourceRevision DataHash
            Cataloged LastSyncDir LastSync TDKeyMapKey CheckOutUser CheckOutStatus CheckedOut
            CheckedIn Expires ETag
        """,
        "REAL": """
            CostImpact
        """,
        "INTEGER": """
            AttachedRevID AttachSeq ApprRevID LastRevID BinSize sfGenerated RefreshBookmarks
            HasSignTabs Confidential IsInherited CloudSync CloudBlockIn CloudBlockOut DocLinks
            RCLinks
        """,
    },
    "sf_document_dialog": {
        "TEXT": """
            MenuID CommandName CommandArgument HasPermits IconImageUrl ItemText InfoText
            DefaultValue HRef HrefTarget UCModule UCFunction Confirm Choices Items
        """,
        "INTEGER": """
            Enabled NeedPermits MenuSeq HideifDisabled
        """,
    },
    "sf_catalog_file": {
        "TEXT": """
            id value type FileType FileKey FolderKey DisplaySize date DocDate Due ReferenceDate
            Project ProjectName DocNo SourceRevision SourceBatchNo DivisionID From FromUser
            AttachNote AttachedToDMK LinkedItemKey ProcessType DocTypeKey MD5 Keywords Other
            SourceNumber SourceBatch SourceContact_dv SourceContact TemplateKey TemplateKey_dv
            ResponsibleParty StatusText SubtypeText CheckOutStatus CheckedOut CheckedIn CheckOutUser
            CheckOutUser_dv AltFileContact_dv AltFileContact Users data ETag
        """,
        "REAL": """
            CostImpact GeoLat GeoLng
        """,
        "INTEGER": """
            size LatestRevision ApprovedRevision Priority Probability Confidential HasDynamicData
            CanEdit CanRename CanView
        """,
    },
    "sf_catalog_version": {
        "TEXT": """
            FileVerKey DataHash SourceRevision Cataloged Approved ApprovedBy FromUser TxtData
            NoCanDelete ETag
        """,
        "INTEGER": """
            RevID BinSize IsCurrentApprovedVersion
        """,
    },
    "sf_catalog_access": {
        "TEXT": """
            UserName FileName Accessed AccessType WithDocument AccessInfo UserKey DocMasterKey ETag
        """,
        "INTEGER": """
            RevID UsedCache
        """,
    },
    "sf_document_search": {
        "TEXT": """
            DocMasterKey DocTypeKey DocReference DocDate DocNo DocBatchNo SourceDocNo Title FromUser
            SortFrom ResponsibleParty_dv ResponsibleParty Company Company_dv ToUser SortTo Author
            Due Signoff Closed Project Specification SubContract PayControl Subtype Subtype_dv
            Status Status_dv Subsegment ContractType SourceDate PayItemNumber ExternalDocNo
            SourceContact_dv SourceContact Section DraftNumber csDate Reason SOVItemNumber
            BudgetRevFlag InPeriod OwnerApprover ArchProject Segment csCode ProjEntity csString016
            csString030 csString040 csString050 csString060 csString080 csString100 csString120
            csString240 DivisionID Source Location LastStatusBy_dv LastStatusBy ETag
        """,
        "REAL": """
            CostImpact csAmount Bond csQty csValue
        """,
        "INTEGER": """
            Priority Confidential Area DaysRequested csNumber Probability Final DocEdit UpdateMask
            csFlag NumToForward DocFlag DaysApproved Duration csCheck NumToSend CurrentSeq MaxStage
            FilesAttached
        """,
    },
    "sf_doc_type_summary": {
        "TEXT": """
            DocTypeKey DocType SOPLink ETag
        """,
        "INTEGER": """
            cnt_open cnt_closed cnt_overdue cnt_DueSoon DaysTillDue CanAdd
        """,
    },
    "sf_choice": {
        "TEXT": """
            SetName Code Description NextSet ETag
        """,
        "INTEGER": """
            Active OnAdd CodeFlag
        """,
    },
    "sf_suggestion": {
        "TEXT": """
            key label value
        """,
    },
    "sf_uicfg_field": {
        "TEXT": """
            PartConfigKey ItemName DataMember DataField DataType Label DisplayFormat LookupName
            ClickAction ParentFilters HelpText RXPattern CSS DV DependsOn Overlay ShowInfoPop
            ShownWhen SortFlag ClientFilter ValidateAgainst ValidateTextAgainst ValidationMax
            ValidationMin ValidationMode WidthCSS UIType ETag OtherProperties
        """,
        "INTEGER": """
            LimitTo MaxChars RequiredBefore SeqData IsReadOnly Visible VisibleLocked
            IsInternalDefault HTML Width
        """,
    },
    "sf_report": {
        "TEXT": """
            MenuID CommandName CommandArgument HasPermits IconImageUrl ItemText InfoText
            DefaultValue HRef HrefTarget UCModule UCFunction Confirm Choices Items HideifDisabled
        """,
        "INTEGER": """
            Enabled NeedPermits MenuSeq
        """,
    },
    "sf_project_cost_committed": {
        "TEXT": """
            RowKey acct AcctClass AcctType Acct_TranClass BaseCuryId batch_id batch_type
            bill_batch_id CpnyId crtd_datetime crtd_prog crtd_user data1 FiscalNo GLAcct GLSubAcct
            lupd_datetime lupd_prog lupd_user part_number pjt_entity po_date project promise_date
            PONumber request_date SourceNum system_cd trans_date tr_comment tr_status Subcontract
            tr_id01 InvoiceNumber UOM user1 VendorId voucher_num emp_name EmployeeId LaborClass name
            equip_id Descr DocMasterKey DocTitle DocDate ETag
        """,
        "REAL": """
            amount units Rate
        """,
        "INTEGER": """
            detail_num voucher_line IsXTS
        """,
    },
    "sf_project_cost_transaction": {
        "TEXT": """
            RowKey acct alloc_flag BaseCuryId batch_id batch_type bill_batch_id CpnyId crtd_datetime
            crtd_prog crtd_user data1 employee fiscalno gl_acct gl_subacct lupd_datetime lupd_prog
            lupd_user pjt_entity post_date project Subcontract system_cd trans_date tr_comment
            tr_id01 InvoiceNumber PONumber SourceBatchNumber LaborClass tr_id23 tr_status
            unit_of_measure vendor_num voucher_num emp_name name equip_id invtid lotsernbr siteid
            whseloc Descr DocMasterKey DocTitle Acct_Type Acct_TranClass Acct_Class ETag
        """,
        "REAL": """
            amount units
        """,
        "INTEGER": """
            detail_num voucher_line IsXTS
        """,
    },
}


def _api_columns(table: str) -> List[Tuple[str, str]]:
    """[(field, affinity)] in declaration order."""
    out: List[Tuple[str, str]] = []
    for affinity, names in _API_COLUMNS.get(table, {}).items():
        out.extend((name, affinity) for name in names.split())
    return out


# --- how each table is keyed --------------------------------------------------
# `local`   our own columns, snake_case, holding the joins Spitfire does not put in the payload
#           (an /items row carries no DocMasterKey) plus the provenance stamp.
# `pk`      may mix local and API columns. Where the payload has no dependable key, `row_sha256`
#           is used: a hash of the row's own content, so re-fetching the same data re-writes the
#           same row instead of accumulating duplicates.
# `indexes` only the ones a query actually takes — every index is paid for on all 618 POs.

_PROVENANCE = (
    ("sweep_id", "INTEGER NOT NULL DEFAULT 0"),
    ("fetched_at", "TEXT NOT NULL DEFAULT ''"),
    ("raw_sha256", "TEXT NOT NULL DEFAULT ''"),   # -> sf_raw_response, the verbatim body
)
_DOC = (("doc_master_key", "TEXT NOT NULL DEFAULT ''"),
        ("po_number", "TEXT NOT NULL DEFAULT ''"))

_TABLES: Dict[str, Dict[str, Any]] = {
    "sf_document": {
        "local": (("po_number", "TEXT NOT NULL DEFAULT ''"),
                  ("project_code", "TEXT NOT NULL DEFAULT ''")) + _PROVENANCE,
        "pk": ("DocMasterKey",),
        "indexes": (("ix_sf_document_po", "po_number"),
                    ("ix_sf_document_project", "project_code")),
    },
    "sf_document_item": {
        "local": _DOC + _PROVENANCE,
        "pk": ("DocItemKey",),
        "indexes": (("ix_sf_item_doc", "doc_master_key"),
                    ("ix_sf_item_po", "po_number"),
                    # The spec code lives in SourceItemNumber, not Specification — the trap
                    # `connectors/spitfire._to_po_line` documents. Matching reads it constantly.
                    ("ix_sf_item_source_no", "SourceItemNumber")),
    },
    "sf_item_task": {
        "local": (("doc_item_key", "TEXT NOT NULL DEFAULT ''"),) + _DOC + _PROVENANCE,
        "pk": ("ItemTaskKey",),
        "indexes": (("ix_sf_task_item", "doc_item_key"),),
    },
    "sf_item_related": {
        # RelatedLineDetails is one object per line and carries no key of its own that is reliably
        # populated, so the line's key is the primary key. It is also the most valuable table here:
        # ContractUnits / ReceivedUnits / ReceiptInProgressUnits are the three quantities an
        # outstanding-balance calculation has to net off, and ItemQuantity on the parent row reads
        # 0.0 on lines that genuinely order units.
        "local": (("doc_item_key", "TEXT NOT NULL DEFAULT ''"),) + _DOC + _PROVENANCE,
        "pk": ("doc_item_key",),
        "indexes": (("ix_sf_related_po", "po_number"),),
    },
    "sf_item_revision_map": {
        "local": (("doc_item_key", "TEXT NOT NULL DEFAULT ''"),) + _DOC + _PROVENANCE,
        "pk": ("DocRevItemKey",),
        "indexes": (),
    },
    "sf_document_address": {
        # AddrType: T=vendor/To, S=ship-to, F=from/author, R=remit-to.
        "local": _DOC + _PROVENANCE,
        "pk": ("DocAddrKey",),
        "indexes": (("ix_sf_addr_doc", "doc_master_key, AddrType"),),
    },
    "sf_document_route": {
        # RouteID restarts per document, so it is unique only alongside the document key.
        "local": _DOC + _PROVENANCE,
        "pk": ("doc_master_key", "RouteID"),
        "indexes": (("ix_sf_route_user", "UserName"),),
    },
    "sf_document_date": {
        "local": _DOC + _PROVENANCE,
        "pk": ("DocDateRowKey",),
        "indexes": (),
    },
    "sf_document_attachment": {
        # Two row shapes in one collection: a file link populates DocKey (a catalog file key) with
        # a null AttachedDocMaster; a document link does the reverse.
        "local": _DOC + _PROVENANCE,
        "pk": ("DocAttachKey",),
        "indexes": (("ix_sf_attach_doc", "doc_master_key"),
                    ("ix_sf_attach_file", "DocKey")),
    },
    "sf_document_comment": {
        "local": _DOC + (("row_sha256", "TEXT NOT NULL DEFAULT ''"),) + _PROVENANCE,
        "pk": ("row_sha256",),
        "indexes": (("ix_sf_comment_doc", "doc_master_key"),),
    },
    "sf_document_dialog": {
        # The 13 dialog/* endpoints all answer 200 and all return the same MenuCommand shape, so
        # they share one table discriminated by `dialog`. MenuID repeats across dialogs.
        "local": (("dialog", "TEXT NOT NULL DEFAULT ''"),) + _DOC + _PROVENANCE,
        "pk": ("doc_master_key", "dialog", "MenuID"),
        "indexes": (),
    },
    "sf_document_search": {
        # The discovery index: what `POST /api/project/{id}/docs` says exists, before anything is
        # fetched. Kept as its own table because a document listed here but never read is exactly
        # the gap a resumed sweep has to see.
        "local": (("project_code", "TEXT NOT NULL DEFAULT ''"),) + _PROVENANCE,
        "pk": ("project_code", "DocMasterKey"),
        "indexes": (("ix_sf_search_docno", "DocNo"),),
    },
    "sf_catalog_file": {
        "local": (("file_key", "TEXT NOT NULL DEFAULT ''"),) + _PROVENANCE,
        "pk": ("file_key",),
        "indexes": (),
    },
    "sf_catalog_version": {
        # DataHash is a server-computed MD5 of the bytes — the only real proof a file is intact.
        "local": (("file_key", "TEXT NOT NULL DEFAULT ''"),) + _PROVENANCE,
        "pk": ("FileVerKey",),
        "indexes": (("ix_sf_version_file", "file_key"),),
    },
    "sf_catalog_access": {
        "local": (("file_key", "TEXT NOT NULL DEFAULT ''"),
                  ("row_sha256", "TEXT NOT NULL DEFAULT ''")) + _PROVENANCE,
        "pk": ("row_sha256",),
        "indexes": (("ix_sf_access_file", "file_key"),),
    },
    "sf_doc_type_summary": {
        "local": (("project_code", "TEXT NOT NULL DEFAULT ''"),) + _PROVENANCE,
        "pk": ("project_code", "DocTypeKey"),
        "indexes": (),
    },
    "sf_choice": {
        # The only route to any business code list — the OpenAPI document declares no enums at all.
        # Normalise on Description, never Code: the 24 UOM rows contain Lot/LT, Set/ST, Allo/Allow.
        "local": (("set_name", "TEXT NOT NULL DEFAULT ''"),
                  ("for_doc_type", "TEXT NOT NULL DEFAULT ''")) + _PROVENANCE,
        "pk": ("set_name", "for_doc_type", "Code"),
        "indexes": (),
    },
    "sf_suggestion": {
        "local": (("lookup_name", "TEXT NOT NULL DEFAULT ''"),
                  ("data_context", "TEXT NOT NULL DEFAULT ''")) + _PROVENANCE,
        "pk": ("lookup_name", "data_context", "key"),
        "indexes": (),
    },
    "sf_uicfg_field": {
        # The field dictionary the OpenAPI document does not contain: every field's Label,
        # DataMember, DataField and MaxChars. DataMember is what /api/history/{m}/{f}/{row} needs,
        # and a wrong one returns 200 [] rather than an error.
        "local": (("part_name", "TEXT NOT NULL DEFAULT ''"),) + _PROVENANCE,
        "pk": ("part_name", "DataMember", "DataField"),
        "indexes": (),
    },
    "sf_report": {
        # Names and HRefs only. These are SSRS behind sfReportViewer.aspx, not REST — the endpoint
        # lists them, nothing here can render one.
        "local": (("report_set", "TEXT NOT NULL DEFAULT ''"),) + _PROVENANCE,
        "pk": ("report_set", "MenuID"),
        "indexes": (),
    },
    "sf_project_cost_committed": {
        # No DocItemKey and no received quantity, so useless for matching; kept as a totals
        # cross-check. po_date is 1900-01-01 on every training row because the ERP peer is not
        # configured — do not read it.
        "local": (("project_code", "TEXT NOT NULL DEFAULT ''"),) + _PROVENANCE,
        "pk": ("project_code", "RowKey"),
        "indexes": (),
    },
    "sf_project_cost_transaction": {
        "local": (("project_code", "TEXT NOT NULL DEFAULT ''"),) + _PROVENANCE,
        "pk": ("project_code", "RowKey"),
        "indexes": (),
    },
}

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe(name: str) -> str:
    """A column name we are willing to put in DDL, or "".

    `_widen` builds ALTER statements out of keys the server sent us, so this is the one place an
    injection could enter. Every field measured against the live API matches; anything that does
    not is dropped rather than escaped, and the raw body still holds it either way.
    """
    return name if _IDENTIFIER.match(name or "") else ""


def _quoted(name: str) -> str:
    """`_safe` admits reserved words — sf_document_route really has a column called `From` —
    so every generated identifier is quoted. `_safe` has already ruled out a quote character
    in the name, which is what makes this safe rather than merely conventional."""
    return '"%s"' % name


def _is_guid_column(name: str) -> bool:
    """Columns holding a Spitfire GUID, which must compare case-insensitively.

    Not a style preference — the same document key arrives in different cases from different
    endpoints. `POST /api/viewable/DocMasterAlt` answers
    `03CCCDD7-32D0-40D9-8DAC-AB14F36305C3` where the document's own header calls itself
    `03cccdd7-32d0-40d9-8dac-ab14f36305c3`. Without NOCASE the two are different keys: the same
    purchase order would insert twice and `sf_document_item` would not join to `sf_document`.
    """
    return name.endswith("Key") or name.endswith("_key") or name in ("DocKey", "row_sha256")


def _decl(name: str, affinity: str) -> str:
    if affinity.startswith("TEXT") and _is_guid_column(name):
        return affinity + " COLLATE NOCASE"
    return affinity


def _columns(table: str) -> List[Tuple[str, str]]:
    """Every declared column: our own first, then the API's, in declaration order."""
    spec = _TABLES[table]
    cols: List[Tuple[str, str]] = [(name, _decl(name, decl)) for name, decl in spec["local"]]
    have = {name for name, _ in cols}
    for name, affinity in _api_columns(table):
        if name not in have and _safe(name):
            cols.append((name, _decl(name, affinity)))
            have.add(name)
    return cols


def _create_sql(table: str) -> str:
    spec = _TABLES[table]
    body = ",\n            ".join(
        _quoted(name) + " " + decl for name, decl in _columns(table))
    pk = ", ".join(_quoted(c) for c in spec["pk"])
    return f"CREATE TABLE IF NOT EXISTS {table} (\n            {body},\n" \
           f"            PRIMARY KEY ({pk})\n        )"


# --- the database -------------------------------------------------------------


def get_connection(db_path=SPITFIRE_WAREHOUSE_DB_PATH) -> sqlite3.Connection:
    """db_path may be a real Path (default) or ":memory:" for tests.

    Mirrors `pipeline/state_db.get_connection`: WAL so a long sweep does not lock out a reader
    browsing the warehouse, and a busy timeout set per connection because it is not persisted.
    """
    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")

    # One sweep = one run of the sync tool. Every row in every table carries its `sweep_id`, so
    # "when was this read, and by which run" is answerable without a second lookup, and a resumed
    # run is distinguishable from the run it resumed.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sf_sweep (
            sweep_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at     TEXT NOT NULL,
            finished_at    TEXT,
            status         TEXT NOT NULL DEFAULT 'RUNNING',
            base_url       TEXT NOT NULL DEFAULT '',
            server_version TEXT NOT NULL DEFAULT '',
            user_key       TEXT NOT NULL DEFAULT '',
            user_email     TEXT NOT NULL DEFAULT '',
            args           TEXT NOT NULL DEFAULT '',
            resumed_from   INTEGER,
            calls_ok       INTEGER NOT NULL DEFAULT 0,
            calls_failed   INTEGER NOT NULL DEFAULT 0,
            docs_done      INTEGER NOT NULL DEFAULT 0,
            docs_failed    INTEGER NOT NULL DEFAULT 0,
            note           TEXT NOT NULL DEFAULT ''
        )
    """)

    # The checkpoint that makes a three-hour sweep survivable. `ref` is a DocMasterKey for scope
    # 'document', a project code for 'project', a file key for 'catalog'. A row is written DONE
    # only after that unit's data is committed, so --resume can trust it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sf_sweep_progress (
            sweep_id   INTEGER NOT NULL,
            scope      TEXT NOT NULL,
            ref        TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'PENDING',
            attempts   INTEGER NOT NULL DEFAULT 0,
            error      TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (sweep_id, scope, ref)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_sf_progress_status "
                 "ON sf_sweep_progress(sweep_id, scope, status)")

    # Every request issued, whether it worked or not. This is what makes "we only ever read"
    # checkable by Premier rather than merely asserted, and it is where a 500-that-means-not-found
    # is distinguishable from a 500 that means a fault.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sf_call_log (
            call_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            sweep_id     INTEGER NOT NULL DEFAULT 0,
            called_at    TEXT NOT NULL DEFAULT '',
            method       TEXT NOT NULL DEFAULT '',
            path         TEXT NOT NULL DEFAULT '',
            payload      TEXT NOT NULL DEFAULT '',
            status       INTEGER,
            bytes        INTEGER NOT NULL DEFAULT 0,
            elapsed_ms   INTEGER NOT NULL DEFAULT 0,
            body_sha256  TEXT NOT NULL DEFAULT '',
            error        TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_sf_calls_sweep ON sf_call_log(sweep_id, status)")

    # The archive, content-addressed. 618 documents return a great many byte-identical bodies —
    # every empty /comments is the same two bytes — so keying on the hash rather than on the call
    # collapses them to one row. gzip because these are JSON: ~10:1, which is the difference
    # between a 30 MB file and a 300 MB one.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sf_raw_response (
            body_sha256  TEXT PRIMARY KEY,
            method       TEXT NOT NULL DEFAULT '',
            path         TEXT NOT NULL DEFAULT '',
            status       INTEGER,
            content_type TEXT NOT NULL DEFAULT '',
            raw_bytes    INTEGER NOT NULL DEFAULT 0,
            body_gz      BLOB,
            first_seen_at TEXT NOT NULL DEFAULT '',
            last_seen_at  TEXT NOT NULL DEFAULT '',
            seen_count    INTEGER NOT NULL DEFAULT 0
        )
    """)

    for table in _TABLES:
        conn.execute(_create_sql(table))
        for index_name, cols in _TABLES[table]["indexes"]:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({cols})")
        _widen(conn, table, [name for name, _ in _columns(table)])

    conn.commit()
    return conn


def _existing_columns(conn: sqlite3.Connection, table: str) -> Dict[str, str]:
    return {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _widen(conn: sqlite3.Connection, table: str, keys: Iterable[str]) -> List[str]:
    """Add a column for every key the table does not have yet. Returns what was added.

    Called with the declared columns on open, and again with the keys of each response as it
    arrives. The server has changed build three times during this project; a field it starts
    sending after this schema was written must land in a column, not be dropped on the floor.
    A column we never declared is added with **no type at all**, which in sqlite means BLOB
    affinity: the value is stored exactly as given, so a number stays a number. Declaring it TEXT
    instead would coerce a boolean into the string "1", and `WHERE NewFlag = 1` would then quietly
    match nothing. We do not know the type of a field we have never seen; saying so is better than
    guessing wrong.
    """
    declared = dict(_api_columns(table))
    existing = _existing_columns(conn, table)
    added = []
    for key in keys:
        name = _safe(key)
        if not name or name in existing:
            continue
        decl = _decl(name, declared[name]) if name in declared else ""
        conn.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {decl}'.rstrip())
        existing[name] = decl
        added.append(name)
    return added


# --- writing ------------------------------------------------------------------


def _coerce(value: Any) -> Any:
    """One API value as something sqlite will store.

    Booleans become 0/1 rather than being left to sqlite's own handling, so a `WHERE Confidential
    = 1` behaves the same on a row written from JSON `true` as on one written from `1`. A nested
    object that survives to here — an endpoint that grew a sub-structure since this was written —
    is stored as its JSON text rather than raising: the sweep must not die on a new field.
    """
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return value


def row_sha256(payload: Any) -> str:
    """A stable hash of a row, for the tables Spitfire gives no key of their own."""
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save_rows(conn: sqlite3.Connection, table: str, rows: Sequence[Any],
              local: Optional[Dict[str, Any]] = None) -> int:
    """Upsert API rows into `table`, stamping each with `local`. Returns rows written.

    `INSERT OR REPLACE` rather than a merge: a re-sweep is meant to *correct*, and a line that has
    genuinely disappeared from a document should not be left behind for a query to find — the same
    reasoning `spitfire_mirror.save_po` gives for deleting a PO's lines before re-inserting them.
    """
    if not rows:
        return 0
    local = dict(local or {})
    seen_keys: List[str] = []
    for row in rows:
        if isinstance(row, dict):
            seen_keys.extend(k for k in row if not isinstance(row[k], (dict, list)))
    _widen(conn, table, dict.fromkeys(seen_keys))

    written = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        values = dict(local)
        if "row_sha256" in _TABLES[table]["pk"]:
            values["row_sha256"] = row_sha256(row)
        for key, value in row.items():
            name = _safe(key)
            if name and not isinstance(value, (dict, list)):
                values[name] = _coerce(value)
        names = list(values)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({', '.join(_quoted(n) for n in names)}) "
            f"VALUES ({', '.join('?' * len(names))})",
            [values[n] for n in names],
        )
        written += 1
    return written


def archive(conn: sqlite3.Connection, method: str, path: str, status: Optional[int],
            content_type: str, body: bytes, now: str) -> str:
    """Store one verbatim response body, deduplicated by content. Returns its sha256."""
    digest = hashlib.sha256(body or b"").hexdigest()
    row = conn.execute(
        "SELECT seen_count FROM sf_raw_response WHERE body_sha256 = ?", (digest,)).fetchone()
    if row:
        conn.execute("UPDATE sf_raw_response SET last_seen_at = ?, seen_count = ? "
                     "WHERE body_sha256 = ?", (now, row[0] + 1, digest))
    else:
        conn.execute(
            "INSERT INTO sf_raw_response (body_sha256, method, path, status, content_type, "
            "raw_bytes, body_gz, first_seen_at, last_seen_at, seen_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (digest, method, path, status, content_type, len(body or b""),
             gzip.compress(body or b"", 6), now, now))
    return digest


def read_archived(conn: sqlite3.Connection, digest: str) -> Optional[bytes]:
    """The verbatim body back out again — what the fidelity test compares columns against."""
    row = conn.execute(
        "SELECT body_gz FROM sf_raw_response WHERE body_sha256 = ?", (digest,)).fetchone()
    return gzip.decompress(row[0]) if row and row[0] is not None else None


def log_call(conn: sqlite3.Connection, sweep_id: int, called_at: str, method: str, path: str,
             payload: Any = None, status: Optional[int] = None, size: int = 0,
             elapsed_ms: int = 0, body_sha256: str = "", error: str = "") -> None:
    conn.execute(
        "INSERT INTO sf_call_log (sweep_id, called_at, method, path, payload, status, bytes, "
        "elapsed_ms, body_sha256, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sweep_id, called_at, method, path,
         json.dumps(payload, separators=(",", ":"))[:2000] if payload is not None else "",
         status, size, elapsed_ms, body_sha256, str(error)[:500]))


# --- the sweep ledger ---------------------------------------------------------


def start_sweep(conn: sqlite3.Connection, now: str, base_url: str, args: str,
                server_version: str = "", user_key: str = "", user_email: str = "",
                resumed_from: Optional[int] = None) -> int:
    cur = conn.execute(
        "INSERT INTO sf_sweep (started_at, status, base_url, server_version, user_key, "
        "user_email, args, resumed_from) VALUES (?, 'RUNNING', ?, ?, ?, ?, ?, ?)",
        (now, base_url, server_version, user_key, user_email, args, resumed_from))
    conn.commit()
    return int(cur.lastrowid)


def finish_sweep(conn: sqlite3.Connection, sweep_id: int, now: str, status: str,
                 note: str = "") -> None:
    counts = conn.execute(
        "SELECT SUM(status BETWEEN 200 AND 299), SUM(status IS NULL OR status >= 300) "
        "FROM sf_call_log WHERE sweep_id = ?", (sweep_id,)).fetchone()
    docs = conn.execute(
        "SELECT SUM(status = 'DONE'), SUM(status = 'FAILED') FROM sf_sweep_progress "
        "WHERE sweep_id = ? AND scope = 'document'", (sweep_id,)).fetchone()
    conn.execute(
        "UPDATE sf_sweep SET finished_at = ?, status = ?, note = ?, calls_ok = ?, "
        "calls_failed = ?, docs_done = ?, docs_failed = ? WHERE sweep_id = ?",
        (now, status, note, counts[0] or 0, counts[1] or 0, docs[0] or 0, docs[1] or 0, sweep_id))
    conn.commit()


def plan_units(conn: sqlite3.Connection, sweep_id: int, scope: str, refs: Iterable[str],
               now: str) -> int:
    """Write the to-do list. Existing rows keep their status, so re-planning is safe."""
    written = 0
    for ref in refs:
        conn.execute(
            "INSERT OR IGNORE INTO sf_sweep_progress (sweep_id, scope, ref, status, updated_at) "
            "VALUES (?, ?, ?, 'PENDING', ?)", (sweep_id, scope, ref, now))
        written += 1
    conn.commit()
    return written


def mark_unit(conn: sqlite3.Connection, sweep_id: int, scope: str, ref: str, status: str,
              now: str, error: str = "") -> None:
    conn.execute(
        "INSERT INTO sf_sweep_progress (sweep_id, scope, ref, status, attempts, error, "
        "updated_at) VALUES (?, ?, ?, ?, 1, ?, ?) "
        "ON CONFLICT(sweep_id, scope, ref) DO UPDATE SET status = excluded.status, "
        "attempts = attempts + 1, error = excluded.error, updated_at = excluded.updated_at",
        (sweep_id, scope, ref, status, str(error)[:500], now))


def pending(conn: sqlite3.Connection, sweep_id: int, scope: str) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT ref FROM sf_sweep_progress WHERE sweep_id = ? AND scope = ? AND status != 'DONE' "
        "ORDER BY ref", (sweep_id, scope)).fetchall()]


def final_status(conn: sqlite3.Connection, sweep_id: int, status: str) -> Tuple[str, str]:
    """What a run that thinks it is done should actually be recorded as.

    A run that stopped early is not a finished sweep, however cleanly it stopped. `--limit`,
    `--discover` and a failed document all leave units PENDING, and stamping those DONE makes the
    sweep unresumable — `resumable_sweep` would skip it and the remaining work would be silently
    abandoned rather than picked up next time. This is the bug that lost 616 of 624 documents
    once, which is why the decision lives here and not inline in the CLI.
    """
    if status != "DONE":
        return status, ""
    labels = (("number", "numbers still unprobed"),
              ("document", "documents still unread"),
              ("catalog", "files still unread"))
    remaining = [(len(pending(conn, sweep_id, scope)), label) for scope, label in labels]
    outstanding = [f"{n} {label}" for n, label in remaining if n]
    if outstanding:
        return "INTERRUPTED", ", ".join(outstanding)
    return "DONE", ""


def resumable_sweep(conn: sqlite3.Connection) -> Optional[int]:
    """The most recent sweep that did not finish, or None."""
    row = conn.execute(
        "SELECT sweep_id FROM sf_sweep WHERE status IN ('RUNNING', 'INTERRUPTED') "
        "ORDER BY sweep_id DESC LIMIT 1").fetchone()
    return int(row[0]) if row else None


def table_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Row counts for every warehouse table, for `--status` and the end-of-run report."""
    out: Dict[str, int] = {}
    for table in list(_TABLES) + ["sf_raw_response", "sf_call_log", "sf_sweep",
                                  "sf_sweep_progress"]:
        try:
            out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            out[table] = -1
    return out
