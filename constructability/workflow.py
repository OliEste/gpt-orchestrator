"""
Constructability workflow implementation - rewritten with state machine and hard guarantees.
"""
from typing import Dict, Optional, List, Any
import sys
import os
import base64
import time
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
import threading
from dataclasses import dataclass, asdict
from enum import Enum
import logging

# Add parent directory to path for shared imports
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from shared.gpt_chain import GPTChain
from shared.responses_api import ResponsesAPIClient
from shared.file_converter import excel_to_structured_data
import requests
from shared.config import SharedConfig
from urllib.parse import quote
from config import WORKFLOW_CONFIG, GPT_1_CONFIG, GPT_2_CONFIG, GPT_3_CONFIG, GPT_4_CONFIG, GPT_5_CONFIG, GPT_6_CONFIG, GPT_7_CONFIG, GPT_8_CONFIG, GPT_9_CONFIG, GPT_10_CONFIG
import io
import tempfile
import re
import PyPDF2
from pdf2image import convert_from_path
import tiktoken
from logging_config import get_logger

# Get logger for this module (will use the root logger configured in orchestrator.py)
logger = get_logger(__name__)


class Phase(Enum):
    """Workflow phases."""
    INDEXING = 1
    TRIAGE = 2
    RENDERING = 3
    REVIEW = 4
    COMPLETE = 5
    ERROR = 6


class FileState(Enum):
    """Individual file processing states."""
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


@dataclass
class IndexPacket:
    """Lightweight index information for a document."""
    path: str
    doc_type: str  # "pdf", "dwg", "xls", "xlsx", "text", "csv", etc.
    page_count: int = 0  # For PDFs: actual pages. For others: 1 (single reviewable unit)
    sheet_hints: List[str] = None  # Sheet names (Excel), layout names (DWG), or filename hints
    text_samples: List[tuple] = None  # [(page_num, text_snippet)]
    metadata: Dict[str, Any] = None  # Additional metadata (sheet names, column headers, etc.)
    
    # New fields for rich manifest
    size_bytes: int = 0
    modified_time: str = None
    folder_context: str = None  # e.g., "/Plans/", "/Specs/"
    pdf_outline: List[Dict] = None  # PDF bookmarks/outline
    sheet_numbers: List[str] = None  # Detected sheet numbers (A1.0, M2.1, etc.)
    index_pages: List[int] = None  # Pages that look like index/cover
    excel_headers: Dict[str, List[str]] = None  # sheet_name -> [headers]
    
    error: Optional[str] = None
    
    def __post_init__(self):
        if self.sheet_hints is None:
            self.sheet_hints = []
        if self.text_samples is None:
            self.text_samples = []
        if self.metadata is None:
            self.metadata = {}
        if self.pdf_outline is None:
            self.pdf_outline = []
        if self.sheet_numbers is None:
            self.sheet_numbers = []
        if self.index_pages is None:
            self.index_pages = []
        if self.excel_headers is None:
            self.excel_headers = {}


@dataclass
class JobState:
    """Persisted job state."""
    job_id: str
    status: str = "running"
    phase: Phase = Phase.INDEXING
    stage: str = "indexing"
    created_at: str = None
    updated_at: str = None
    
    # Inputs
    file_paths: List[str] = None
    selected_disciplines: List[str] = None
    
    # Per-file state
    files: Dict[str, Dict[str, Any]] = None
    
    # Phase outputs
    triage_results: Dict[str, List[tuple]] = None
    rendered_pages: Dict[tuple, str] = None
    review_results: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow().isoformat()
        if self.updated_at is None:
            self.updated_at = datetime.utcnow().isoformat()
        if self.files is None:
            self.files = {}
        if self.triage_results is None:
            self.triage_results = {}
        if self.rendered_pages is None:
            self.rendered_pages = {}
        if self.review_results is None:
            self.review_results = {}
    
    def to_dict(self):
        """Convert to dictionary for persistence."""
        d = asdict(self)
        d['phase'] = self.phase.value
        return d
    
    @classmethod
    def from_dict(cls, d):
        """Create from dictionary."""
        d['phase'] = Phase(d['phase'])
        return cls(**d)


class ConstructabilityWorkflow:
    """
    Constructability review workflow with state machine and hard guarantees.
    """
    
    # No limits - process all files completely
    
    def __init__(self):
        self.workflow_config = WORKFLOW_CONFIG
        self.knowledge_data = {}
        self.excel_hints_cache = {}
        self.file_content_cache = {}
    
    def _load_knowledge_files(self) -> Dict[str, Any]:
        """Load knowledge files (Master Observations and Checklist) from shared/knowledge folder.
        """
        knowledge_data = {
            'master_observations': None,
            'checklist': None
        }
        
        # Get knowledge folder path
        knowledge_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'shared',
            'knowledge'
        )
        
        master_obs_path = os.path.join(knowledge_dir, 'Master Observation List.xlsx')
        checklist_path = os.path.join(knowledge_dir, 'Coastal Document Review Checklist.xlsx')
        
        try:
            # Load Master Observations List
            if os.path.exists(master_obs_path):
                with open(master_obs_path, 'rb') as f:
                    master_obs_bytes = f.read()
                knowledge_data['master_observations'] = excel_to_structured_data(
                    master_obs_bytes, 
                    master_obs_path
                )
                logger.info(f"Loaded Master Observation List ({len(knowledge_data['master_observations'].get('sheets', []))} sheets)")
            else:
                logger.warning(f"Master Observation List not found at {master_obs_path}")
            
            # Load Checklist
            if os.path.exists(checklist_path):
                with open(checklist_path, 'rb') as f:
                    checklist_bytes = f.read()
                knowledge_data['checklist'] = excel_to_structured_data(
                    checklist_bytes,
                    checklist_path
                )
                logger.info(f"Loaded Coastal Document Review Checklist ({len(knowledge_data['checklist'].get('sheets', []))} sheets)")
            else:
                logger.warning(f"Coastal Document Review Checklist not found at {checklist_path}")
        
        except Exception as e:
            logger.error(f"Error loading knowledge files: {e}")
        
        return knowledge_data
    
    def execute_constructability_review(
        self,
        file_paths: List[str],
        selected_disciplines: List[str],
        egnyte_token: str,
        progress_callback: Optional[callable] = None,
        job_state: Optional[JobState] = None
    ) -> Dict:
        """
        Execute the constructability review workflow with state machine.
        
        Args:
            file_paths: List of Egnyte file paths
            selected_disciplines: List of discipline names
            egnyte_token: Egnyte OAuth token
            progress_callback: Optional progress callback
            job_state: Optional existing job state (for resumption)
        
        Returns:
            Dictionary with review results
        """
        # Initialize or resume job state
        if job_state is None:
            job_state = JobState(
                job_id="",  # Will be set by caller
                file_paths=file_paths,
                selected_disciplines=selected_disciplines
            )
            # Initialize file states
            for file_path in file_paths:
                job_state.files[file_path] = {
                    "state": FileState.QUEUED.value,
                    "index_packet": None,
                    "error": None
                }
        
        # Load knowledge files once at workflow start
        if not self.knowledge_data:
            self.knowledge_data = self._load_knowledge_files()
        
        try:
            # Phase 1: Indexing
            if job_state.phase == Phase.INDEXING:
                job_state = self._phase1_indexing(job_state, egnyte_token, progress_callback)
            
            # Phase 2: Triage
            if job_state.phase == Phase.TRIAGE:
                job_state = self._phase2_triage(job_state, egnyte_token, progress_callback)
            
            # Phase 3: Render selected pages
            if job_state.phase == Phase.RENDERING:
                job_state = self._phase3_rendering(job_state, egnyte_token, progress_callback)
            
            # Phase 4: Review
            if job_state.phase == Phase.REVIEW:
                job_state = self._phase4_review(job_state, egnyte_token, progress_callback)
            
            # Phase 6: Complete
            if job_state.phase == Phase.COMPLETE:
                return self._build_final_result(job_state)
            
        except Exception as e:
            job_state.phase = Phase.ERROR
            job_state.status = "error"
            job_state.stage = f"error: {str(e)}"
            job_state.updated_at = datetime.utcnow().isoformat()
            raise
        
        return self._build_final_result(job_state)
    
    def _phase1_indexing(
        self,
        job_state: JobState,
        egnyte_token: str,
        progress_callback: Optional[callable]
    ) -> JobState:
        """
        Phase 1: Index files using Egnyte listing API metadata only.
        NO file downloads - only folder listing metadata.
        """
        logger.info("[PHASE 1] Starting metadata-only indexing")
        job_state.stage = "indexing"
        job_state.updated_at = datetime.utcnow().isoformat()
        
        if progress_callback:
            progress_callback({
                'phase': 1,
                'stage': 'indexing',
                'current_stage': 'Phase 1: Metadata Indexing'
            })
        
        if not job_state.file_paths:
            logger.info("[PHASE 1] No files to index, proceeding to Phase 2")
            job_state.phase = Phase.TRIAGE
            return job_state
        
        # Extract root folder from first file (assume all files in same root folder)
        first_file = job_state.file_paths[0]
        root_folder = '/'.join(first_file.split('/')[:-1]) if '/' in first_file else '/'
        
        logger.info(f"[PHASE 1] Fetching file metadata from Egnyte listing API for folder: {root_folder}")
        
        # Use Egnyte listing API to get all file metadata
        try:
            from orchestrator import _list_files_recursive
            all_files_metadata = _list_files_recursive(egnyte_token, root_folder)
        except Exception as e:
            logger.error(f"[PHASE 1] Error fetching file metadata: {e}")
            # Fallback to basic indexing
            all_files_metadata = []
        
        # Create metadata map
        metadata_map = {f['path']: f for f in all_files_metadata}
        
        # Index all files using metadata only
        indexed_count = 0
        for file_path in job_state.file_paths:
            file_metadata = metadata_map.get(file_path, {})
            
            # Create IndexPacket from metadata only (no download)
            try:
                index_packet_dict = self._index_file_metadata_only(file_path, file_metadata)
                job_state.files[file_path] = {
                    "state": FileState.DONE.value,
                    "index_packet": index_packet_dict,
                    "error": None
                }
                indexed_count += 1
            except Exception as e:
                job_state.files[file_path] = {
                    "state": FileState.ERROR.value,
                    "index_packet": None,
                    "error": str(e)
                }
                logger.error(f"[PHASE 1] {os.path.basename(file_path)}: {e}")
        
        logger.info(f"[PHASE 1] Indexed {indexed_count} files using metadata only (no downloads)")
        
        job_state.phase = Phase.TRIAGE
        job_state.stage = "triage_pass1"
        job_state.updated_at = datetime.utcnow().isoformat()
        return job_state
    
    @staticmethod
    def _index_file_isolated(file_path: str, egnyte_token: str) -> Dict:
        """
        Index a single file WITHOUT fetching - lightweight metadata extraction.
        Files will be fetched on-demand in Phase 3 when GPTs request them.
        This avoids hitting Egnyte API rate limits (1,000 calls/day).
        """
        try:
            file_ext = os.path.splitext(file_path)[1].lower()
            filename = os.path.basename(file_path)
            
            # Infer file type from extension - NO API CALLS
            if file_ext == '.pdf':
                return asdict(IndexPacket(
                    path=file_path,
                    doc_type="pdf",
                    page_count=0,
                    sheet_hints=[filename],
                    metadata={"lazy_loaded": True}
                ))
            elif file_ext in ['.xls', '.xlsx']:
                return asdict(IndexPacket(
                    path=file_path,
                    doc_type="xls" if file_ext == '.xls' else "xlsx",
                    page_count=1,
                    sheet_hints=[filename],
                    metadata={"lazy_loaded": True, "sheet_names": []}
                ))
            elif file_ext == '.dwg':
                return asdict(IndexPacket(
                    path=file_path,
                    doc_type="dwg",
                    page_count=1,
                    sheet_hints=[filename],
                    metadata={"lazy_loaded": True, "needs_conversion": True}
                ))
            elif file_ext in ['.doc', '.docx']:
                return asdict(IndexPacket(
                    path=file_path,
                    doc_type="doc" if file_ext == '.doc' else "docx",
                    page_count=1,
                    sheet_hints=[filename],
                    metadata={"lazy_loaded": True}
                ))
            elif file_ext in ['.txt', '.csv', '.json', '.xml']:
                return asdict(IndexPacket(
                    path=file_path,
                    doc_type="text",
                    page_count=1,
                    sheet_hints=[filename],
                    metadata={"lazy_loaded": True}
                ))
            else:
                return asdict(IndexPacket(
                    path=file_path,
                    doc_type="unknown",
                    page_count=1,
                    sheet_hints=[filename],
                    metadata={"lazy_loaded": True}
                ))
        
        except Exception as e:
            return asdict(IndexPacket(
                path=file_path,
                doc_type="error",
                page_count=1,
                sheet_hints=[os.path.basename(file_path)],
                metadata={"error": str(e), "lazy_loaded": True},
                error=str(e)
            ))
    
    @staticmethod
    def _index_file_metadata_only(file_path: str, file_metadata: Dict) -> Dict:
        """
        Create IndexPacket from Egnyte listing metadata only.
        NO file downloads - pure metadata extraction.
        """
        try:
            file_ext = os.path.splitext(file_path)[1].lower()
            filename = os.path.basename(file_path)
            
            # Extract folder context
            folder_context = os.path.dirname(file_path)
            if folder_context:
                parts = folder_context.split('/')
                if len(parts) > 2:
                    folder_context = '/' + '/'.join(parts[-2:])
            
            # Get metadata from Egnyte listing
            size_bytes = file_metadata.get('size', 0)
            modified_time = file_metadata.get('last_modified', None)
            
            # Determine doc type from extension
            if file_ext == '.pdf':
                doc_type = "pdf"
                page_count = 0
            elif file_ext in ['.xls', '.xlsx']:
                doc_type = "xls" if file_ext == '.xls' else "xlsx"
                page_count = 1
            elif file_ext == '.dwg':
                doc_type = "dwg"
                page_count = 1
            elif file_ext in ['.doc', '.docx']:
                doc_type = "doc" if file_ext == '.doc' else "docx"
                page_count = 1
            elif file_ext in ['.txt', '.csv', '.json', '.xml']:
                doc_type = "text"
                page_count = 1
            else:
                doc_type = file_ext[1:] if file_ext else "unknown"
                page_count = 1
            
            return asdict(IndexPacket(
                path=file_path,
                doc_type=doc_type,
                page_count=page_count,
                sheet_hints=[filename],
                size_bytes=size_bytes,
                modified_time=modified_time,
                folder_context=folder_context,
                metadata={
                    "lazy_loaded": True,
                    "metadata_only": True,
                    "needs_hints": doc_type in ["pdf", "xls", "xlsx"]
                }
            ))
        
        except Exception as e:
            return asdict(IndexPacket(
                path=file_path,
                doc_type="error",
                page_count=1,
                sheet_hints=[os.path.basename(file_path)],
                metadata={"error": str(e)},
                error=str(e)
            ))
    
    def _phase2_triage(
        self,
        job_state: JobState,
        egnyte_token: str,
        progress_callback: Optional[callable]
    ) -> JobState:
        """
        Phase 2: Two-pass triage system.
        Pass 1: Metadata-only manifest, GPTs select files
        Pass 2: Fetch minimal hints for selected files, refine selection
        """
        logger.info("[PHASE 2] Starting two-pass triage")
        job_state.stage = "triage_pass1"
        job_state.updated_at = datetime.utcnow().isoformat()
        
        if progress_callback:
            progress_callback({
                'phase': 2,
                'stage': 'triage_pass1',
                'current_stage': 'Phase 2: Triage Pass 1 (Metadata Only)'
            })
        
        # Build indexed files list
        indexed_files = [
            (path, IndexPacket(**data["index_packet"]))
            for path, data in job_state.files.items()
            if data["state"] == FileState.DONE.value and data["index_packet"]
        ]
        
        if not indexed_files:
            logger.warning("[PHASE 2] No indexed files available")
            job_state.phase = Phase.COMPLETE
            return job_state
        
        # Filter out DWG files
        indexed_files = [(fp, ip) for fp, ip in indexed_files if ip.doc_type != "dwg"]
        
        # ============================================
        # PASS 1: Metadata-Only Triage
        # ============================================
        logger.info("[PHASE 2] PASS 1: Building metadata-only manifest")
        manifest_pass1 = self._build_metadata_only_manifest(indexed_files)
        
        DISCIPLINE_MAP = {
            "Fire Protection": GPT_2_CONFIG,
            "Civil": GPT_3_CONFIG,
            "Plumbing": GPT_4_CONFIG,
            "Electrical": GPT_5_CONFIG,
            "Structural": GPT_6_CONFIG,
            "Architectural": GPT_7_CONFIG,
            "Mechanical": GPT_8_CONFIG,
        }
        
        api_client = ResponsesAPIClient()
        discipline_errors = {}
        pass1_selections = {}
        
        # Pass 1: GPTs select files using metadata only (parallel processing)
        def _triage_pass1_discipline(discipline: str) -> tuple[str, Optional[set], Optional[str]]:
            """Helper function for Pass 1 triage - can be called in parallel."""
            if discipline not in DISCIPLINE_MAP:
                return (discipline, None, f"Unknown discipline: {discipline}")
            
            try:
                logger.info(f"[PHASE 2] PASS 1: {discipline} selecting files from metadata")
                gpt_config = DISCIPLINE_MAP[discipline]
                model_name = gpt_config.get('model', 'gpt-5.1')
                
                # Get token encoder for the model
                try:
                    # Try to get encoding for the model, fallback to o200k_base for newer models
                    if 'gpt-5' in model_name or 'o1' in model_name or 'o200k' in model_name.lower():
                        encoder = tiktoken.get_encoding("o200k_base")
                    else:
                        encoder = tiktoken.encoding_for_model(model_name)
                except:
                    # Fallback to cl100k_base (GPT-4 style) if model-specific encoding not found
                    encoder = tiktoken.get_encoding("cl100k_base")
                
                # Build prompt template without manifest
                prompt_template = f"""You are performing a {discipline} constructability review.

Below is a manifest of available project files with metadata only (no content downloaded yet).

{{MANIFEST_PLACEHOLDER}}

IMPORTANT: Review this manifest and select ONLY the files that are relevant to {discipline} constructability review.

Selection criteria:
- Discipline cues in filenames (e.g., A-, AD-, ARCH for Architectural; FP-, FP for Fire Protection)
- Folder location (e.g., "/Plans/Architectural" vs "/Archive/Old")
- File size and recency (prefer larger, more recent files)
- File type (PDFs for drawings, Excel for schedules)

Respond with a list of FILE_PATH values (one per line):
FILE_PATH: <full path>

Only include files that are clearly relevant. If uncertain, include it (we'll refine in Pass 2).

Your response should be ONLY the list of FILE_PATH values, one per line."""
                
                user_input_text = "Please select the files you want to review."
                
                # Calculate token limits
                max_tokens = 272000
                safety_margin = 0.9
                max_input_tokens = int(max_tokens * safety_margin)
                
                # Count tokens in prompt template and user input (without manifest)
                template_tokens = len(encoder.encode(prompt_template.replace("{MANIFEST_PLACEHOLDER}", "")))
                user_input_tokens = len(encoder.encode(user_input_text))
                available_manifest_tokens = max_input_tokens - template_tokens - user_input_tokens
                
                # Check if manifest needs truncation
                manifest_tokens = len(encoder.encode(manifest_pass1))
                manifest_to_use = manifest_pass1
                
                if manifest_tokens > available_manifest_tokens:
                    logger.warning(f"Manifest too large for {discipline}: {manifest_tokens:,} tokens (limit: {available_manifest_tokens:,}). Truncating...")
                    manifest_to_use = self._truncate_manifest_to_token_limit(
                        indexed_files, available_manifest_tokens, encoder
                    )
                    truncated_tokens = len(encoder.encode(manifest_to_use))
                    logger.info(f"Truncated manifest to {truncated_tokens:,} tokens for {discipline}")
                else:
                    logger.info(f"Manifest token count for {discipline}: {manifest_tokens:,} tokens (limit: {available_manifest_tokens:,})")
                
                # Build final prompt with manifest
                triage_prompt = prompt_template.replace("{MANIFEST_PLACEHOLDER}", manifest_to_use)
                
                response = api_client.create_response(
                    system_prompt=triage_prompt,
                    user_input=user_input_text,
                    model=model_name,
                    temperature=0.3
                )
                
                # Parse response to get selected file paths
                selected_paths = self._parse_file_selection(response, indexed_files)
                
                logger.info(f"[PHASE 2] PASS 1: {discipline} selected {len(selected_paths)} files")
                return (discipline, selected_paths, None)
            
            except Exception as e:
                error_msg = str(e)
                logger.error(f"[PHASE 2] PASS 1: Error in {discipline} triage: {e}")
                return (discipline, None, error_msg)
        
        # Process all disciplines in parallel for Pass 1
        with ThreadPoolExecutor(max_workers=len(job_state.selected_disciplines)) as executor:
            pass1_futures = {
                executor.submit(_triage_pass1_discipline, discipline): discipline
                for discipline in job_state.selected_disciplines
            }
            
            for future in as_completed(pass1_futures):
                discipline, selected_paths, error = future.result()
                if error:
                    discipline_errors[discipline] = error
                elif selected_paths is not None:
                    pass1_selections[discipline] = selected_paths
        
        # Collect all files selected in Pass 1
        all_pass1_files = set()
        for selected_paths in pass1_selections.values():
            all_pass1_files.update(selected_paths)
        
        logger.info(f"[PHASE 2] PASS 1: Total {len(all_pass1_files)} unique files selected across all disciplines")
        
        # ============================================
        # PASS 2: Determine which disciplines need Pass 2
        # ============================================
        # Track which files have hints (Excel files)
        files_with_hints = set()
        
        # Categorize disciplines: those with Excel files need Pass 2
        disciplines_needing_pass2 = {}
        disciplines_skipping_pass2 = {}
        
        for discipline, pass1_files in pass1_selections.items():
            # Check if this discipline selected any Excel files
            has_excel_files = False
            for file_path in pass1_files:
                index_packet = next((ip for fp, ip in indexed_files if fp == file_path), None)
                if index_packet and index_packet.doc_type in ["xls", "xlsx"]:
                    has_excel_files = True
                    files_with_hints.add(file_path)
                    break
            
            if has_excel_files:
                disciplines_needing_pass2[discipline] = pass1_files
            else:
                disciplines_skipping_pass2[discipline] = pass1_files
                # Skip Pass 2, convert Pass 1 directly to pages
                logger.info(f"[PHASE 2] PASS 2: {discipline} selected only PDFs (no hints), skipping Pass 2 GPT call")
                selected_pages = self._convert_files_to_pages(pass1_files, indexed_files)
                job_state.triage_results[discipline] = selected_pages
        
        logger.info(f"[PHASE 2] PASS 2: {len(disciplines_skipping_pass2)} disciplines skipping Pass 2 (PDF-only), {len(disciplines_needing_pass2)} disciplines need Pass 2 (have Excel files)")
        
        # ============================================
        # PASS 2: Fetch Minimal Hints for Selected Files (Excel only)
        # ============================================
        if progress_callback:
            progress_callback({
                'phase': 2,
                'stage': 'triage_pass2',
                'current_stage': 'Phase 2: Triage Pass 2 (Fetching Hints)'
            })
        
        # Only fetch hints for files that will be used in Pass 2
        files_needing_hints = set()
        for pass1_files in disciplines_needing_pass2.values():
            files_needing_hints.update(pass1_files)
        
        if files_needing_hints:
            logger.info(f"[PHASE 2] PASS 2: Fetching minimal hints for {len(files_needing_hints)} files (Excel only)")
            
            # Fetch hints only for files that need Pass 2
            for file_path in files_needing_hints:
                # Find the index_packet
                index_packet = None
                for fp, ip in indexed_files:
                    if fp == file_path:
                        index_packet = ip
                        break
                
                if not index_packet:
                    continue
                
                # Fetch minimal hints based on file type
                if index_packet.doc_type == "pdf" and index_packet.metadata.get("needs_hints"):
                    # Skip PDF hints
                    index_packet.metadata["needs_hints"] = False
                
                elif index_packet.doc_type in ["xls", "xlsx"] and index_packet.metadata.get("needs_hints"):
                    hints = self._fetch_excel_hints(file_path, egnyte_token, index_packet.modified_time)
                    if hints:
                        index_packet.sheet_hints = hints.get('sheet_names', [])
                        index_packet.excel_headers = hints.get('headers', {})
                        index_packet.metadata["needs_hints"] = False
                        files_with_hints.add(file_path)
        else:
            logger.info("[PHASE 2] PASS 2: No files need hints (all disciplines are PDF-only)")
        
        # Build per-discipline manifests (only files with hints for each discipline)
        logger.info("[PHASE 2] PASS 2: Building per-discipline manifests with hints")
        discipline_manifests = {}
        
        for discipline, pass1_files in disciplines_needing_pass2.items():
            # Only include Excel files with hints from this discipline's selection
            files_with_hints_for_discipline = [
                (fp, ip) for fp, ip in indexed_files 
                if fp in pass1_files and fp in files_with_hints
            ]
            
            if files_with_hints_for_discipline:
                discipline_manifests[discipline] = self._build_enhanced_manifest_with_hints(
                    files_with_hints_for_discipline
                )
            else:
                discipline_manifests[discipline] = None
        
        # Pass 2: Refine selections with hints (only for disciplines with Excel files)
        def _triage_pass2_discipline(discipline: str) -> tuple[str, Optional[List[tuple]], Optional[str]]:
            """Helper function for Pass 2 triage - can be called in parallel."""
            if discipline not in DISCIPLINE_MAP or discipline not in disciplines_needing_pass2:
                return (discipline, None, "Not in DISCIPLINE_MAP or no Excel files selected")
            
            try:
                logger.info(f"[PHASE 2] PASS 2: {discipline} refining selection with hints")
                gpt_config = DISCIPLINE_MAP[discipline]
                
                # Get Pass 1 selection
                pass1_files = disciplines_needing_pass2[discipline]
                
                # Get discipline-specific manifest (only Excel files with hints)
                discipline_manifest = discipline_manifests.get(discipline)
                
                if not discipline_manifest:
                    # No hints available, fallback to Pass 1
                    selected_pages = self._convert_files_to_pages(pass1_files, indexed_files)
                    return (discipline, selected_pages, "No hints available")
                
                triage_prompt = f"""You are performing a {discipline} constructability review.

In Pass 1, you selected these files:
{chr(10).join(f'FILE_PATH: {fp}' for fp in pass1_files)}

Below are content hints (sheet names, headers) for the Excel files you selected:

{discipline_manifest}

IMPORTANT: 
- For Excel files: Select specific sheets if only certain sheets are relevant (use format: FILE_PATH: <path> (sheets: Sheet1, Sheet3))
- For PDF files: Keep your Pass 1 selection (all pages) - page content will be reviewed in Phase 4
- Remove any files that are clearly not relevant after seeing hints

Respond with a list of FILE_PATH values (one per line):
- For all pages: FILE_PATH: <full path> (all pages)
- For specific pages: FILE_PATH: <full path> (pages: 1, 3, 5-10)

Your response should be ONLY the list of FILE_PATH values, one per line."""
                
                response = api_client.create_response(
                    system_prompt=triage_prompt,
                    user_input="Please refine your file selection based on the content hints.",
                    model=gpt_config.get('model', 'gpt-5.1'),
                    temperature=0.3
                )
                
                # Parse refined selection
                selected_pages = self._parse_file_selection_with_pages(response, indexed_files)
                
                logger.info(f"[PHASE 2] PASS 2: {discipline} selected {len(selected_pages)} pages")
                return (discipline, selected_pages, None)
            
            except Exception as e:
                # Fallback to Pass 1 selection
                error_msg = str(e)
                logger.error(f"[PHASE 2] PASS 2: Error in {discipline}, using Pass 1 selection: {e}")
                selected_pages = self._convert_files_to_pages(disciplines_needing_pass2[discipline], indexed_files)
                return (discipline, selected_pages, error_msg)
        
        # Process only disciplines that need Pass 2 (those with Excel files)
        pass2_selections = {}
        disciplines_to_process = list(disciplines_needing_pass2.keys())
        
        if disciplines_to_process:
            logger.info(f"[PHASE 2] PASS 2: Running Pass 2 for {len(disciplines_to_process)} disciplines with Excel files")
            with ThreadPoolExecutor(max_workers=len(disciplines_to_process)) as executor:
                pass2_futures = {
                    executor.submit(_triage_pass2_discipline, discipline): discipline
                    for discipline in disciplines_to_process
                }
                
                for future in as_completed(pass2_futures):
                    discipline, selected_pages, error = future.result()
                    if selected_pages is not None:
                        pass2_selections[discipline] = selected_pages
                        job_state.triage_results[discipline] = selected_pages
                    if error:
                        logger.warning(f"[PHASE 2] PASS 2: {discipline} had error (used fallback): {error}")
        else:
            logger.info("[PHASE 2] PASS 2: No disciplines need Pass 2 (all selected only PDFs)")
        
        # Master Observations and Checklist: Use union of all discipline selections
        all_selected_files = set()
        for pages in job_state.triage_results.values():
            all_selected_files.update(set(f for f, _ in pages))
        
        all_selected_pages = []
        for file_path in all_selected_files:
            index_packet = next((ip for fp, ip in indexed_files if fp == file_path), None)
            if not index_packet or index_packet.doc_type == "dwg":
                continue
            
            if index_packet.doc_type == "pdf":
                if index_packet.page_count > 0:
                    all_selected_pages.extend([(file_path, p) for p in range(1, index_packet.page_count + 1)])
                else:
                    all_selected_pages.append((file_path, 1))
            else:
                all_selected_pages.append((file_path, 1))
        
        if self.knowledge_data.get('master_observations'):
            job_state.triage_results['Master Observations'] = all_selected_pages
            logger.info(f"[PHASE 2] Master Observations: Using {len(all_selected_pages)} pages from {len(all_selected_files)} files (union of discipline selections)")
        
        if self.knowledge_data.get('checklist'):
            job_state.triage_results['Checklist Review'] = all_selected_pages
            logger.info(f"[PHASE 2] Checklist Review: Using {len(all_selected_pages)} pages from {len(all_selected_files)} files (union of discipline selections)")
        
        job_state.phase = Phase.RENDERING
        job_state.updated_at = datetime.utcnow().isoformat()
        return job_state
    
    def _build_metadata_only_manifest(self, indexed_files: List[tuple]) -> str:
        """Build manifest using only metadata (no content hints)."""
        lines = []
        for file_path, index_packet in indexed_files:
            filename = os.path.basename(file_path)
            lines.append(f"=== FILE: {filename} ===")
            lines.append(f"FILE_PATH: {file_path}")
            lines.append(f"Type: {index_packet.doc_type.upper()}")
            lines.append(f"Size: {index_packet.size_bytes:,} bytes" if index_packet.size_bytes else "Size: unknown")
            lines.append(f"Folder: {index_packet.folder_context}" if index_packet.folder_context else "")
            lines.append(f"Modified: {index_packet.modified_time}" if index_packet.modified_time else "")
            lines.append("")
        return "\n".join(lines)
    
    def _truncate_manifest_to_token_limit(
        self, 
        indexed_files: List[tuple], 
        max_tokens: int, 
        encoder: tiktoken.Encoding
    ) -> str:
        """Build manifest but truncate if it exceeds token limit."""
        lines = []
        current_tokens = 0
        files_included = 0
        
        for file_path, index_packet in indexed_files:
            filename = os.path.basename(file_path)
            file_entry_lines = [
                f"=== FILE: {filename} ===",
                f"FILE_PATH: {file_path}",
                f"Type: {index_packet.doc_type.upper()}",
                f"Size: {index_packet.size_bytes:,} bytes" if index_packet.size_bytes else "Size: unknown",
                f"Folder: {index_packet.folder_context}" if index_packet.folder_context else "",
                f"Modified: {index_packet.modified_time}" if index_packet.modified_time else "",
                ""
            ]
            file_entry = "\n".join(file_entry_lines)
            entry_tokens = len(encoder.encode(file_entry))
            
            if current_tokens + entry_tokens > max_tokens:
                logger.warning(f"Manifest truncated: {files_included} files included, {len(indexed_files) - files_included} files excluded")
                break
                
            lines.extend(file_entry_lines)
            current_tokens += entry_tokens
            files_included += 1
        
        return "\n".join(lines)
    
    def _build_enhanced_manifest_with_hints(self, indexed_files: List[tuple]) -> str:
        """Build manifest with content hints for selected files."""
        lines = []
        for file_path, index_packet in indexed_files:
            filename = os.path.basename(file_path)
            lines.append(f"=== FILE: {filename} ===")
            lines.append(f"FILE_PATH: {file_path}")
            lines.append(f"Type: {index_packet.doc_type.upper()}")
            lines.append(f"Size: {index_packet.size_bytes:,} bytes" if index_packet.size_bytes else "")
            lines.append(f"Folder: {index_packet.folder_context}" if index_packet.folder_context else "")
            
            if index_packet.doc_type == "pdf":
                # Page count will be discovered during Phase 3 rendering
                if index_packet.page_count > 0:
                    lines.append(f"Pages: {index_packet.page_count}")
                else:
                    lines.append("Pages: unknown (will be determined when rendered)")
            
            elif index_packet.doc_type in ["xls", "xlsx"]:
                if index_packet.sheet_hints:
                    lines.append(f"Sheets: {', '.join(index_packet.sheet_hints[:10])}")
                if index_packet.excel_headers:
                    lines.append("Headers:")
                    for sheet, headers in list(index_packet.excel_headers.items())[:3]:
                        lines.append(f"  {sheet}: {', '.join(headers[:5])}")
            
            lines.append("")
        return "\n".join(lines)
    
    @staticmethod
    def _fetch_pdf_hints(file_path: str, egnyte_token: str) -> Optional[Dict]:
        """Fetch minimal PDF hints using Range request (first 50KB only)."""
        try:
            egnyte_base_url = SharedConfig.EGNYTE_BASE_URL.rstrip('/')
            encoded_path = quote(file_path, safe='/')
            file_url = f"{egnyte_base_url}/pubapi/v1/fs-content{encoded_path}"
            headers = {
                'Authorization': f'Bearer {egnyte_token}',
                'Range': 'bytes=0-51200'
            }
            response = requests.get(file_url, headers=headers, timeout=30)
            response.raise_for_status()
            pdf_partial = response.content
            
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_partial))
            page_count = len(pdf_reader.pages) if hasattr(pdf_reader, 'pages') else 0
            
            outline = []
            if hasattr(pdf_reader, 'outline') and pdf_reader.outline:
                for item in pdf_reader.outline[:10]:
                    if isinstance(item, list):
                        continue
                    outline.append({
                        "title": item.title if hasattr(item, 'title') else str(item),
                        "page": getattr(item, 'page', None)
                    })
            
            text_samples = []
            sheet_numbers = []
            index_pages = []
            
            if page_count > 0:
                try:
                    page = pdf_reader.pages[0]
                    text = page.extract_text() or ""
                    if text:
                        text_samples.append((1, text[:500].replace('\n', ' ').strip()))
                        sheet_pattern = r'\b([A-Z]{1,3}[-.]?\d+\.?\d*)\b'
                        matches = re.findall(sheet_pattern, text, re.IGNORECASE)
                        sheet_numbers = list(set(matches[:10]))
                        index_keywords = ['SHEET INDEX', 'GENERAL NOTES', 'COVER', 'TITLE', 'INDEX']
                        if any(kw in text.upper() for kw in index_keywords):
                            index_pages = [1]
                except Exception:
                    pass
            
            return {
                'page_count': page_count,
                'outline': outline,
                'text_samples': text_samples,
                'sheet_numbers': sheet_numbers,
                'index_pages': index_pages
            }
        except Exception as e:
            logger.warning(f"[PHASE 2] Could not fetch PDF hints for {file_path}: {e}")
            return None
    
    def _fetch_excel_hints(self, file_path: str, egnyte_token: str, modified_time: Optional[str] = None) -> Optional[Dict]:
        """Fetch minimal Excel hints (sheet names and headers only) with caching.
        Also caches file content for Phase 3 reuse."""
        # Check cache first
        cache_key = (file_path, modified_time)
        if cache_key in self.excel_hints_cache:
            logger.debug(f"[PHASE 2] Using cached Excel hints for {os.path.basename(file_path)}")
            return self.excel_hints_cache[cache_key]
        
        try:
            egnyte_base_url = SharedConfig.EGNYTE_BASE_URL.rstrip('/')
            encoded_path = quote(file_path, safe='/')
            file_url = f"{egnyte_base_url}/pubapi/v1/fs-content{encoded_path}"
            headers = {'Authorization': f'Bearer {egnyte_token}'}
            response = requests.get(file_url, headers=headers, timeout=30)
            response.raise_for_status()
            excel_bytes = response.content
            
            # Cache file content for Phase 3 reuse
            if modified_time:
                self.file_content_cache[cache_key] = excel_bytes
                logger.debug(f"[PHASE 2] Cached Excel file content for Phase 3: {os.path.basename(file_path)}")
            
            import pandas as pd
            excel_file = pd.ExcelFile(io.BytesIO(excel_bytes))
            sheet_names = excel_file.sheet_names
            
            headers = {}
            
            for sheet_name in sheet_names[:10]:
                try:
                    df = pd.read_excel(excel_file, sheet_name=sheet_name, nrows=1)
                    headers[sheet_name] = [str(h) for h in df.columns.tolist()[:10]]
                except Exception:
                    continue
            
            hints = {
                'sheet_names': sheet_names,
                'headers': headers
            }
            
            # Cache the hints
            self.excel_hints_cache[cache_key] = hints
            return hints
        except Exception as e:
            logger.warning(f"[PHASE 2] Could not fetch Excel hints for {file_path}: {e}")
            return None
    
    def _parse_file_selection(self, response: str, indexed_files: List[tuple]) -> List[str]:
        """Parse GPT response to extract selected file paths (Pass 1)."""
        selected_paths = []
        lines = response.strip().split('\n')
        
        # Create mapping for quick lookup
        basename_to_paths = {}
        for file_path, index_packet in indexed_files:
            basename = os.path.basename(file_path)
            if basename not in basename_to_paths:
                basename_to_paths[basename] = []
            basename_to_paths[basename].append((file_path, index_packet))
        
        for line in lines:
            line = line.strip()
            if not line or not line.startswith('FILE_PATH:'):
                continue
            
            # Extract file path
            path_part = line.replace('FILE_PATH:', '').strip()
            
            # Try exact match first
            file_path = None
            for fp, ip in indexed_files:
                if fp == path_part or fp.endswith(path_part):
                    file_path = fp
                    break
            
            # If not found, try basename match
            if not file_path:
                basename = os.path.basename(path_part)
                if basename in basename_to_paths:
                    file_path, _ = basename_to_paths[basename][0]
            
            if file_path:
                selected_paths.append(file_path)
        
        return list(set(selected_paths))
    
    def _parse_file_selection_with_pages(self, response: str, indexed_files: List[tuple]) -> List[tuple]:
        """Parse GPT response to extract selected file paths with page specifications (Pass 2)."""
        selected_pages = []
        lines = response.strip().split('\n')
        
        # Create mapping for quick lookup
        basename_to_paths = {}
        for file_path, index_packet in indexed_files:
            basename = os.path.basename(file_path)
            if basename not in basename_to_paths:
                basename_to_paths[basename] = []
            basename_to_paths[basename].append((file_path, index_packet))
        
        for line in lines:
            line = line.strip()
            if not line or not line.startswith('FILE_PATH:'):
                continue
            
            # Extract file path and page spec
            path_part = line.replace('FILE_PATH:', '').strip()
            
            # Check if pages are specified
            pages_spec = None
            if '(pages:' in path_part.lower() or '(all pages)' in path_part.lower():
                if '(all pages)' in path_part.lower():
                    pages_spec = 'all'
                    path_part = path_part.split('(all pages)')[0].strip()
                else:
                    pages_match = re.search(r'\(pages:\s*([^)]+)\)', path_part, re.IGNORECASE)
                    if pages_match:
                        pages_spec = pages_match.group(1).strip()
                        path_part = path_part.split('(pages:')[0].strip()
            
            # Find matching file path
            file_path = None
            index_packet = None
            
            # Try exact match first
            for fp, ip in indexed_files:
                if fp == path_part or fp.endswith(path_part):
                    file_path = fp
                    index_packet = ip
                    break
            
            # If not found, try basename match
            if not file_path:
                basename = os.path.basename(path_part)
                if basename in basename_to_paths:
                    file_path, index_packet = basename_to_paths[basename][0]
            
            if not file_path or not index_packet:
                continue
            
            # Determine which pages to request
            if index_packet.doc_type == "pdf":
                if pages_spec == 'all' or pages_spec is None:
                    # Request all pages
                    if index_packet.page_count > 0:
                        for page_num in range(1, index_packet.page_count + 1):
                            selected_pages.append((file_path, page_num))
                    else:
                        # Unknown page count - request only page 1 initially
                        selected_pages.append((file_path, 1))
                else:
                    # Parse specific pages
                    page_nums = self._parse_page_spec(pages_spec, index_packet.page_count)
                    for page_num in page_nums:
                        selected_pages.append((file_path, page_num))
            else:
                # Excel, text, etc. - request page 1 (entire file)
                selected_pages.append((file_path, 1))
        
        return selected_pages
    
    @staticmethod
    def _parse_page_spec(page_spec: str, max_pages: int) -> List[int]:
        """Parse page specification like '1, 3, 5-10' into list of page numbers."""
        page_nums = []
        parts = page_spec.split(',')
        for part in parts:
            part = part.strip()
            if '-' in part:
                # Range
                start, end = part.split('-', 1)
                try:
                    start_num = int(start.strip())
                    end_num = int(end.strip())
                    if max_pages > 0:
                        end_num = min(end_num, max_pages)
                    page_nums.extend(range(start_num, end_num + 1))
                except ValueError:
                    continue
            else:
                # Single page
                try:
                    page_num = int(part.strip())
                    if max_pages > 0:
                        page_num = min(page_num, max_pages)
                    page_nums.append(page_num)
                except ValueError:
                    continue
        return sorted(set(page_nums))
    
    def _convert_files_to_pages(self, file_paths: List[str], indexed_files: List[tuple]) -> List[tuple]:
        """Convert file paths to page tuples (fallback for Pass 2 errors)."""
        selected_pages = []
        for file_path in file_paths:
            index_packet = next((ip for fp, ip in indexed_files if fp == file_path), None)
            if not index_packet or index_packet.doc_type == "dwg":
                continue
            
            if index_packet.doc_type == "pdf":
                if index_packet.page_count > 0:
                    for page_num in range(1, index_packet.page_count + 1):
                        selected_pages.append((file_path, page_num))
                else:
                    selected_pages.append((file_path, 1))
            else:
                selected_pages.append((file_path, 1))
        
        return selected_pages
    
    def _phase3_rendering(
        self,
        job_state: JobState,
        egnyte_token: str,
        progress_callback: Optional[callable]
    ) -> JobState:
        """Phase 3: Render only selected pages."""
        logger.info("[PHASE 3] Rendering selected pages")
        job_state.stage = "rendering"
        job_state.updated_at = datetime.utcnow().isoformat()
        
        if progress_callback:
            progress_callback({
                'phase': 3,
                'stage': 'rendering',
                'current_stage': 'Phase 3: Rendering pages'
            })
        
        # Collect all unique pages to render
        all_pages = set()
        for discipline, pages in job_state.triage_results.items():
            all_pages.update(pages)
        
        if not all_pages:
            logger.info("[PHASE 3] No pages to render")
            job_state.phase = Phase.REVIEW
            return job_state
        
        # Group pages by file_path and filter out DWG files
        pages_by_file = {}
        skipped_dwg_count = 0
        for file_path, page_num in all_pages:
            # Skip DWG files - GPTs cannot view them
            if file_path.lower().endswith('.dwg'):
                skipped_dwg_count += 1
                logger.info(f"[PHASE 3] Skipping DWG file (not viewable by GPTs): {file_path}")
                continue
            if file_path not in pages_by_file:
                pages_by_file[file_path] = []
            pages_by_file[file_path].append(page_num)
        
        if skipped_dwg_count > 0:
            logger.info(f"[PHASE 3] Skipped {skipped_dwg_count} DWG file(s) from rendering")
        
        logger.info(f"[PHASE 3] Rendering {len(all_pages) - skipped_dwg_count} pages from {len(pages_by_file)} files")
        
        # Use ThreadPoolExecutor for IIS compatibility (ProcessPoolExecutor causes crashes in IIS)
        start_time = time.time()
        rendered_count = 0
        
        # Get modified_time for each file from index_packets for cache lookup
        file_modified_times = {}
        for file_path in pages_by_file.keys():
            if file_path in job_state.files and job_state.files[file_path].get('index_packet'):
                index_packet_dict = job_state.files[file_path]['index_packet']
                file_modified_times[file_path] = index_packet_dict.get('modified_time')
        
        with ThreadPoolExecutor(max_workers=min(5, len(pages_by_file))) as executor:
            # Submit rendering tasks
            future_to_file = {}
            for file_path, page_nums in pages_by_file.items():
                modified_time = file_modified_times.get(file_path)
                future = executor.submit(
                    self._render_file_isolated,
                    file_path,
                    page_nums,
                    egnyte_token,
                    self.file_content_cache,  # Pass cache for reuse
                    modified_time  # Pass modified_time for cache lookup
                )
                future_to_file[future] = (file_path, page_nums)
            
            logger.info(f"[PHASE 3] Submitted {len(future_to_file)} rendering tasks")
            
            # Process all completions with per-file timeout
            RENDER_TIMEOUT = 300
            for future in as_completed(future_to_file.keys()):
                file_path, page_nums = future_to_file[future]
                
                try:
                    # Add timeout to prevent hanging on corrupt PDFs or stuck processes
                    rendered_pages_dict = future.result(timeout=RENDER_TIMEOUT)
                    
                    # Store rendered pages (can be images, Excel data, text, etc.)
                    for (fpath, pnum), content in rendered_pages_dict.items():
                        job_state.rendered_pages[(fpath, pnum)] = content
                        rendered_count += 1
                    
                    logger.info(f"[PHASE 3] {os.path.basename(file_path)}: Processed {len(rendered_pages_dict)}/{len(page_nums)} items")
                    
                except FutureTimeoutError:
                    logger.warning(f"[PHASE 3] {os.path.basename(file_path)}: Timeout after {RENDER_TIMEOUT}s (file may be corrupt or too large)")
                except Exception as e:
                    logger.error(f"[PHASE 3] {os.path.basename(file_path)}: {e}")
                
                # Progress update
                if rendered_count % 5 == 0 or rendered_count == len(all_pages):
                    elapsed = time.time() - start_time
                    logger.info(f"[PHASE 3] Progress: {rendered_count}/{len(all_pages)} pages rendered ({elapsed:.1f}s elapsed)")
                    
                    if progress_callback:
                        progress_callback({
                            'phase': 3,
                            'stage': 'rendering',
                            'current_stage': f'Phase 3: Rendering ({rendered_count}/{len(all_pages)} pages)'
                        })
        
        logger.info(f"[PHASE 3] Complete: Rendered {rendered_count}/{len(all_pages)} pages in {time.time() - start_time:.1f}s")
        
        job_state.phase = Phase.REVIEW
        job_state.updated_at = datetime.utcnow().isoformat()
        return job_state
    
    @staticmethod
    def _render_file_isolated(file_path: str, page_nums: List[int], egnyte_token: str, file_content_cache: Optional[Dict] = None, modified_time: Optional[str] = None) -> Dict[tuple, str]:
        """
        Render pages/sheets from any file type in isolated process.
        Returns: {(file_path, page_num): content}
        - For PDFs: image_base64 string
        - For Excel: JSON-encoded structured data with "EXCEL_DATA:" prefix
        - For text files: text content with "TEXT_DATA:" prefix
        - For other files: base64-encoded content with "BINARY_DATA:" prefix
        """
        try:
            file_ext = os.path.splitext(file_path)[1].lower()
            
            # Check cache first (for Excel files cached in Phase 2)
            file_bytes = None
            if file_content_cache and modified_time:
                cache_key = (file_path, modified_time)
                if cache_key in file_content_cache:
                    file_bytes = file_content_cache[cache_key]
                    logger.debug(f"[PHASE 3] Using cached file content for {os.path.basename(file_path)}")
            
            # Fetch file from Egnyte if not cached
            if file_bytes is None:
                egnyte_base_url = SharedConfig.EGNYTE_BASE_URL.rstrip('/')
                encoded_path = quote(file_path, safe='/')
                file_url = f"{egnyte_base_url}/pubapi/v1/fs-content{encoded_path}"
                
                headers = {'Authorization': f'Bearer {egnyte_token}'}
                
                # Retry logic for 429 errors with exponential backoff
                max_retries = 5
                response = None
                filename = os.path.basename(file_path)
                for attempt in range(max_retries):
                    try:
                        response = requests.get(file_url, headers=headers, timeout=60, stream=True)
                        
                        # Handle rate limiting (429) with exponential backoff
                        if response.status_code == 429:
                            retry_after_header = response.headers.get('Retry-After')
                            if retry_after_header:
                                retry_after = int(retry_after_header)
                            else:
                                retry_after = 2 ** attempt
                            wait_time = min(retry_after, 60)
                            if attempt < max_retries - 1:
                                if retry_after_header:
                                    logger.warning(f"[PHASE 3] 429 Too Many Requests for {filename}. Retry-After: {retry_after}s, Waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                                else:
                                    logger.warning(f"[PHASE 3] 429 Too Many Requests for {filename}. No Retry-After header, using exponential backoff. Waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                                time.sleep(wait_time)
                                continue
                            else:
                                response.raise_for_status()
                        
                        response.raise_for_status()
                        break
                        
                    except requests.exceptions.HTTPError as e:
                        if e.response and e.response.status_code == 429:
                            if attempt < max_retries - 1:
                                retry_after_header = e.response.headers.get('Retry-After')
                                if retry_after_header:
                                    retry_after = int(retry_after_header)
                                else:
                                    retry_after = 2 ** attempt
                                wait_time = min(retry_after, 60)
                                if retry_after_header:
                                    logger.warning(f"[PHASE 3] 429 Too Many Requests for {filename}. Retry-After: {retry_after}s, Waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                                else:
                                    logger.warning(f"[PHASE 3] 429 Too Many Requests for {filename}. No Retry-After header, using exponential backoff. Waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                                time.sleep(wait_time)
                                continue
                        raise
                
                if response is None:
                    raise requests.exceptions.HTTPError("Failed to fetch file after all retries")
                
                # Get file content
                file_bytes = response.content
            
            # Handle PDF files
            if file_ext == '.pdf':
                # Stream to temp file
                fd, tmp_path = tempfile.mkstemp(suffix='.pdf')
                try:
                    with os.fdopen(fd, 'wb') as tmp_file:
                        tmp_file.write(file_bytes)
                    
                    # Discover actual page count (for lazy-loaded files)
                    with open(tmp_path, 'rb') as pdf_file:
                        pdf_reader = PyPDF2.PdfReader(pdf_file)
                        actual_page_count = len(pdf_reader.pages)
                    
                    # Validate requested pages
                    valid_pages = [p for p in page_nums if 1 <= p <= actual_page_count]
                    
                    if not valid_pages:
                        return {}
                    
                    # Render all valid pages
                    min_page = min(valid_pages)
                    max_page = max(valid_pages)
                    images = convert_from_path(tmp_path, dpi=150, first_page=min_page, last_page=max_page)
                    
                    # Convert to base64
                    rendered_pages = {}
                    for image_idx, img in enumerate(images):
                        actual_page_num = min_page + image_idx
                        if actual_page_num in valid_pages:
                            buffered = io.BytesIO()
                            img.save(buffered, format="PNG")
                            image_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                            rendered_pages[(file_path, actual_page_num)] = image_base64
                    
                    return rendered_pages
                finally:
                    try:
                        os.unlink(tmp_path)
                    except:
                        pass
            
            # Handle Excel files - convert to structured data
            elif file_ext in ['.xls', '.xlsx']:
                try:
                    # Convert Excel to structured data
                    structured_data = excel_to_structured_data(file_bytes, file_path)
                    
                    # Store as JSON-encoded string with prefix for Phase 4 to detect
                    excel_json = json.dumps(structured_data)
                    rendered_pages = {}
                    for page_num in page_nums:
                        # Store with special prefix so Phase 4 knows it's Excel data
                        rendered_pages[(file_path, page_num)] = f"EXCEL_DATA:{excel_json}"
                    
                    return rendered_pages
                except Exception as e:
                    logger.error(f"[PHASE 3] Error converting Excel {file_path}: {e}")
                    # Fallback: store as base64
                    file_base64 = base64.b64encode(file_bytes).decode('utf-8')
                    rendered_pages = {}
                    for page_num in page_nums:
                        rendered_pages[(file_path, page_num)] = f"BINARY_DATA:{file_base64}"
                    return rendered_pages
            
            # Handle other file types
            else:
                # For text files, decode and store as text
                # For binary files, store as base64
                try:
                    text_content = file_bytes.decode('utf-8')
                    rendered_pages = {}
                    for page_num in page_nums:
                        rendered_pages[(file_path, page_num)] = f"TEXT_DATA:{text_content}"
                    return rendered_pages
                except:
                    # Binary file - store as base64
                    file_base64 = base64.b64encode(file_bytes).decode('utf-8')
                    rendered_pages = {}
                    for page_num in page_nums:
                        rendered_pages[(file_path, page_num)] = f"BINARY_DATA:{file_base64}"
                    return rendered_pages
        
        except Exception as e:
            return {}
    
    def _phase4_review(
        self,
        job_state: JobState,
        egnyte_token: str,
        progress_callback: Optional[callable]
    ) -> JobState:
        """Phase 4: Visual review by discipline GPTs."""
        logger.info("[PHASE 4] Starting visual review")
        job_state.stage = "review"
        job_state.updated_at = datetime.utcnow().isoformat()
        
        if progress_callback:
            progress_callback({
                'phase': 4,
                'stage': 'review',
                'current_stage': 'Phase 4: Visual review'
            })
        
        # Discipline to GPT config mapping
        DISCIPLINE_MAP = {
            "Fire Protection": GPT_2_CONFIG,
            "Civil": GPT_3_CONFIG,
            "Plumbing": GPT_4_CONFIG,
            "Electrical": GPT_5_CONFIG,
            "Structural": GPT_6_CONFIG,
            "Architectural": GPT_7_CONFIG,
            "Mechanical": GPT_8_CONFIG,
        }
        
        api_client = ResponsesAPIClient()
        discipline_results = {}
        discipline_errors = {}
        BATCH_SIZE = 5
        
        # Thread-safe lock for writing results
        results_lock = threading.Lock()
        
        def process_discipline(discipline: str):
            """Process a single discipline in a thread."""
            if discipline not in DISCIPLINE_MAP:
                with results_lock:
                    discipline_errors[discipline] = f"Unknown discipline: {discipline}"
                return
            
            # Get pages requested by this discipline
            requested_pages = job_state.triage_results.get(discipline, [])
            if not requested_pages:
                logger.info(f"[PHASE 4] {discipline}: No pages requested, skipping")
                return
            
            # Get rendered pages for this discipline
            rendered_pages_for_discipline = [
                (file_path, page_num)
                for file_path, page_num in requested_pages
                if (file_path, page_num) in job_state.rendered_pages
            ]
            
            if not rendered_pages_for_discipline:
                logger.warning(f"[PHASE 4] {discipline}: No rendered pages available")
                return
            
            logger.info(f"[PHASE 4] {discipline}: Reviewing {len(rendered_pages_for_discipline)} pages")
            
            # Batch pages
            batches = []
            for i in range(0, len(rendered_pages_for_discipline), BATCH_SIZE):
                batches.append(rendered_pages_for_discipline[i:i + BATCH_SIZE])
            
            discipline_findings = []
            
            # Process each batch
            for batch_idx, batch in enumerate(batches):
                try:
                    logger.info(f"[PHASE 4] {discipline}: Processing batch {batch_idx + 1}/{len(batches)} ({len(batch)} pages)")
                    
                    # Build vision content for this batch
                    vision_content = []
                    batch_text = f"Review the following {len(batch)} items for {discipline} constructability issues:\n\n"
                    
                    for file_path, page_num in batch:
                        content = job_state.rendered_pages.get((file_path, page_num))
                        if not content:
                            logger.warning(f"[PHASE 4] Warning: No content for {file_path}, page {page_num}")
                            continue
                        
                        filename = os.path.basename(file_path)
                        file_ext = os.path.splitext(file_path)[1].lower()
                        
                        # Detect content type and format appropriately
                        if content.startswith("EXCEL_DATA:"):
                            # Excel file - structured data
                            excel_json_str = content[len("EXCEL_DATA:"):]
                            try:
                                excel_data = json.loads(excel_json_str)
                                
                                # Build text content from Excel sheets
                                excel_text = f"File: {filename}\nPath: {file_path}\nType: Excel file\n\n"
                                
                                for sheet in excel_data.get("sheets", []):
                                    excel_text += f"=== Sheet: {sheet['sheet_name']} ===\n"
                                    excel_text += f"Columns: {', '.join(sheet.get('columns', []))}\n"
                                    excel_text += f"Rows: {sheet.get('row_count', 0)}\n\n"
                                    excel_text += f"CSV Data:\n{sheet.get('content', '')}\n\n"
                                
                                excel_text += f"\nReview this Excel file for {discipline} constructability issues. Check schedules, quantities, and coordination with drawings. Cite observations by file name and sheet name."
                                
                                batch_text += f"- {filename} (Excel file, {len(excel_data.get('sheets', []))} sheets)\n"
                                vision_content.append({
                                    "type": "text",
                                    "text": excel_text
                                })
                            except json.JSONDecodeError as e:
                                logger.error(f"[PHASE 4] Error parsing Excel data for {file_path}: {e}")
                                continue
                        
                        elif content.startswith("TEXT_DATA:"):
                            # Text file
                            text_data = content[len("TEXT_DATA:"):]
                            batch_text += f"- {filename} (Text file)\n"
                            vision_content.append({
                                "type": "text",
                                "text": f"File: {filename}\nPath: {file_path}\nType: Text file\n\n{text_data}\n\nReview this file for {discipline} constructability issues."
                            })
                        
                        elif content.startswith("BINARY_DATA:"):
                            # Binary file (unknown type)
                            batch_text += f"- {filename} (Binary file)\n"
                            vision_content.append({
                                "type": "text",
                                "text": f"File: {filename}\nPath: {file_path}\nType: Binary file ({file_ext})\n\nThis file type may require special handling. Review file name and path for constructability relevance."
                            })
                        
                        else:
                            # Assume it's an image (PDF page)
                            image_base64 = content
                            batch_text += f"- {filename}, Page {page_num}\n"
                            
                            vision_content.append({
                                "type": "text",
                                "text": f"File: {filename}\nPath: {file_path}\nPage: {page_num}\n\nReview this page for {discipline} constructability issues. Cite observations by file name and page number."
                            })
                            vision_content.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_base64}"
                                }
                            })
                    
                    # Call discipline GPT
                    gpt_config = DISCIPLINE_MAP[discipline]
                    response = api_client.create_response(
                        system_prompt=gpt_config.get('instructions', ''),
                        user_input=vision_content,
                        model=gpt_config.get('model', 'gpt-5.1'),
                        temperature=gpt_config.get('temperature', 0.7)
                    )
                    
                    discipline_findings.append({
                        'batch': batch_idx + 1,
                        'pages': batch,
                        'response': response
                    })
                    
                except Exception as e:
                    logger.error(f"[PHASE 4] {discipline}: Error in batch {batch_idx + 1}: {e}")
                    continue
            
            # Store results thread-safely
            result_text = "\n\n".join([
                f"Batch {f['batch']}:\n{f['response']}"
                for f in discipline_findings
            ])
            with results_lock:
                discipline_results[discipline] = result_text
            logger.info(f"[PHASE 4] {discipline}: Complete ({len(discipline_findings)} batches)")
        
        # Process GPT_9: Master Observations Review and GPT_10: Checklist Review in parallel
        master_obs_result = None
        checklist_result = None
        master_obs_error = None
        checklist_error = None
        
        def run_master_observations():
            nonlocal master_obs_result, master_obs_error
            if self.knowledge_data.get('master_observations'):
                try:
                    logger.info("[PHASE 4] Starting Master Observations Review (GPT_9)")
                    master_obs_result = self._process_master_observations_review(
                        job_state, egnyte_token, api_client
                    )
                    if master_obs_result:
                        logger.info("[PHASE 4] Master Observations: Complete")
                except Exception as e:
                    logger.error(f"[PHASE 4] Master Observations: Error: {e}")
                    master_obs_error = str(e)
        
        def run_checklist():
            nonlocal checklist_result, checklist_error
            if self.knowledge_data.get('checklist'):
                try:
                    logger.info("[PHASE 4] Starting Checklist Review (GPT_10)")
                    checklist_result = self._process_checklist_review(
                        job_state, egnyte_token, api_client
                    )
                    if checklist_result:
                        logger.info("[PHASE 4] Checklist Review: Complete")
                except Exception as e:
                    logger.error(f"[PHASE 4] Checklist Review: Error: {e}")
                    checklist_error = str(e)
        
        # Start all threads in parallel: disciplines + Master Observations + Checklist Review
        threads = []
        
        # Start discipline threads
        for discipline in job_state.selected_disciplines:
            discipline_thread = threading.Thread(
                target=process_discipline,
                args=(discipline,),
                name=f"Discipline-{discipline}"
            )
            discipline_thread.start()
            threads.append(discipline_thread)
            logger.info(f"[PHASE 4] Started thread for {discipline}")
        
        # Start Master Observations thread
        if self.knowledge_data.get('master_observations'):
            master_thread = threading.Thread(target=run_master_observations, name="MasterObservations")
            master_thread.start()
            threads.append(master_thread)
            logger.info("[PHASE 4] Started thread for Master Observations")
        
        # Start Checklist Review thread
        if self.knowledge_data.get('checklist'):
            checklist_thread = threading.Thread(target=run_checklist, name="ChecklistReview")
            checklist_thread.start()
            threads.append(checklist_thread)
            logger.info("[PHASE 4] Started thread for Checklist Review")
        
        logger.info(f"[PHASE 4] Waiting for {len(threads)} threads to complete...")
        
        # Wait for all threads to complete
        for thread in threads:
            thread.join()
        
        logger.info("[PHASE 4] All threads completed")
        
        # Collect Master Observations and Checklist results
        if master_obs_result:
            discipline_results['Master Observations'] = master_obs_result
        if master_obs_error:
            discipline_errors['Master Observations'] = master_obs_error
        
        if checklist_result:
            discipline_results['Checklist Review'] = checklist_result
        if checklist_error:
            discipline_errors['Checklist Review'] = checklist_error
        
        # Merge results using GPT_1_CONFIG (consensus GPT)
        merged_output = ""
        if discipline_results:
            try:
                logger.info(f"[PHASE 4] Merging results from {len(discipline_results)} disciplines")
                
                merge_input = "Discipline-specific constructability review results:\n\n"
                for discipline, result in discipline_results.items():
                    merge_input += f"=== {discipline} ===\n{result}\n\n"
                
                merge_input += "\nMerge, deduplicate, and prioritize all findings by severity (Critical, High, Medium, Low). Format as a numbered list with severity in brackets, discipline, reference, and observation. Example: '1. [Critical] Fire Protection: Sheet FP2.01 - Head spacing not dimensioned; compliance with NFPA unclear.'"
                
                merged_output = api_client.create_response(
                    system_prompt=GPT_1_CONFIG.get('instructions', ''),
                    user_input=merge_input,
                    model=GPT_1_CONFIG.get('model', 'gpt-5.1'),
                    temperature=GPT_1_CONFIG.get('temperature', 0.7)
                )
                
                logger.info("[PHASE 4] Merge complete")
            except Exception as e:
                logger.error(f"[PHASE 4] Error merging results: {e}")
                merged_output = "Error merging results"
        
        # Store results
        job_state.review_results = {
            'discipline_results': discipline_results,
            'discipline_errors': discipline_errors,
            'merged_output': merged_output
        }
        
        job_state.phase = Phase.COMPLETE
        job_state.updated_at = datetime.utcnow().isoformat()
        return job_state
    
    def _process_master_observations_review(
        self,
        job_state: JobState,
        egnyte_token: str,
        api_client: ResponsesAPIClient
    ) -> Optional[str]:
        """Process Master Observations review using GPT_9 with knowledge file content."""
        master_obs_data = self.knowledge_data.get('master_observations')
        if not master_obs_data:
            return None
        
        # Build knowledge content text from Excel sheets
        knowledge_text = "=== COASTAL MASTER OBSERVATIONS LIST ===\n\n"
        for sheet in master_obs_data.get('sheets', []):
            knowledge_text += f"Sheet: {sheet['sheet_name']}\n"
            knowledge_text += f"Columns: {', '.join(sheet.get('columns', []))}\n\n"
            knowledge_text += f"{sheet.get('content', '')}\n\n"
        
        # Get rendered pages requested by Master Observations in triage
        requested_pages = job_state.triage_results.get('Master Observations', [])
        rendered_pages = [
            (file_path, page_num)
            for file_path, page_num in requested_pages
            if (file_path, page_num) in job_state.rendered_pages
        ]
        
        if not rendered_pages:
            logger.warning("[PHASE 4] Master Observations: No rendered pages available")
            return None
        
        # Process in batches
        BATCH_SIZE = 5
        batches = []
        for i in range(0, len(rendered_pages), BATCH_SIZE):
            batches.append(rendered_pages[i:i + BATCH_SIZE])
        
        all_findings = []
        
        for batch_idx, batch in enumerate(batches):
            try:
                logger.info(f"[PHASE 4] Master Observations: Processing batch {batch_idx + 1}/{len(batches)} ({len(batch)} items)")
                
                vision_content = []
                batch_text = f"Review the following {len(batch)} project files against the Master Observations List:\n\n"
                batch_text += knowledge_text
                batch_text += "\n=== PROJECT FILES TO REVIEW ===\n\n"
                
                for file_path, page_num in batch:
                    content = job_state.rendered_pages.get((file_path, page_num))
                    if not content:
                        continue
                    
                    filename = os.path.basename(file_path)
                    file_ext = os.path.splitext(file_path)[1].lower()
                    
                    # Format content based on type
                    if content.startswith("EXCEL_DATA:"):
                        excel_json_str = content[len("EXCEL_DATA:"):]
                        try:
                            excel_data = json.loads(excel_json_str)
                            excel_text = f"File: {filename}\nPath: {file_path}\nType: Excel file\n\n"
                            for sheet in excel_data.get("sheets", []):
                                excel_text += f"=== Sheet: {sheet['sheet_name']} ===\n"
                                excel_text += f"CSV Data:\n{sheet.get('content', '')}\n\n"
                            batch_text += f"- {filename} (Excel file)\n"
                            vision_content.append({"type": "text", "text": excel_text})
                        except json.JSONDecodeError:
                            continue
                    elif content.startswith("TEXT_DATA:"):
                        text_data = content[len("TEXT_DATA:"):]
                        batch_text += f"- {filename} (Text file)\n"
                        vision_content.append({
                            "type": "text",
                            "text": f"File: {filename}\nPath: {file_path}\nType: Text file\n\n{text_data}"
                        })
                    else:
                        # PDF image
                        image_base64 = content
                        batch_text += f"- {filename}, Page {page_num}\n"
                        vision_content.append({
                            "type": "text",
                            "text": f"File: {filename}\nPath: {file_path}\nPage: {page_num}\n\nReview this page against the Master Observations List above."
                        })
                        vision_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_base64}"}
                        })
                
                # Prepend knowledge and batch info
                vision_content.insert(0, {"type": "text", "text": batch_text})
                
                # Call GPT_9
                response = api_client.create_response(
                    system_prompt=GPT_9_CONFIG.get('instructions', ''),
                    user_input=vision_content,
                    model=GPT_9_CONFIG.get('model', 'gpt-5.1'),
                    temperature=GPT_9_CONFIG.get('temperature', 0.7)
                )
                
                all_findings.append({
                    'batch': batch_idx + 1,
                    'response': response
                })
                
            except Exception as e:
                logger.error(f"[PHASE 4] Master Observations: Error in batch {batch_idx + 1}: {e}")
                continue
        
        if all_findings:
            return "\n\n".join([
                f"Batch {f['batch']}:\n{f['response']}"
                for f in all_findings
            ])
        return None
    
    def _process_checklist_review(
        self,
        job_state: JobState,
        egnyte_token: str,
        api_client: ResponsesAPIClient
    ) -> Optional[str]:
        """Process Checklist review using GPT_10 with knowledge file content."""
        checklist_data = self.knowledge_data.get('checklist')
        if not checklist_data:
            return None
        
        # Build knowledge content text from Excel sheets
        knowledge_text = "=== COASTAL DOCUMENT REVIEW CHECKLIST ===\n\n"
        for sheet in checklist_data.get('sheets', []):
            knowledge_text += f"Sheet: {sheet['sheet_name']}\n"
            knowledge_text += f"Columns: {', '.join(sheet.get('columns', []))}\n\n"
            knowledge_text += f"{sheet.get('content', '')}\n\n"
        
        # Get rendered pages requested by Checklist Review in triage
        requested_pages = job_state.triage_results.get('Checklist Review', [])
        rendered_pages = [
            (file_path, page_num)
            for file_path, page_num in requested_pages
            if (file_path, page_num) in job_state.rendered_pages
        ]
        
        if not rendered_pages:
            logger.warning("[PHASE 4] Checklist Review: No rendered pages available")
            return None
        
        # Process in batches
        BATCH_SIZE = 5
        batches = []
        for i in range(0, len(rendered_pages), BATCH_SIZE):
            batches.append(rendered_pages[i:i + BATCH_SIZE])
        
        all_findings = []
        
        for batch_idx, batch in enumerate(batches):
            try:
                logger.info(f"[PHASE 4] Checklist Review: Processing batch {batch_idx + 1}/{len(batches)} ({len(batch)} items)")
                
                vision_content = []
                batch_text = f"Review the following {len(batch)} project files against the Coastal Document Review Checklist:\n\n"
                batch_text += knowledge_text
                batch_text += "\n=== PROJECT FILES TO REVIEW ===\n\n"
                
                for file_path, page_num in batch:
                    content = job_state.rendered_pages.get((file_path, page_num))
                    if not content:
                        continue
                    
                    filename = os.path.basename(file_path)
                    file_ext = os.path.splitext(file_path)[1].lower()
                    
                    # Format content based on type
                    if content.startswith("EXCEL_DATA:"):
                        excel_json_str = content[len("EXCEL_DATA:"):]
                        try:
                            excel_data = json.loads(excel_json_str)
                            excel_text = f"File: {filename}\nPath: {file_path}\nType: Excel file\n\n"
                            for sheet in excel_data.get("sheets", []):
                                excel_text += f"=== Sheet: {sheet['sheet_name']} ===\n"
                                excel_text += f"CSV Data:\n{sheet.get('content', '')}\n\n"
                            batch_text += f"- {filename} (Excel file)\n"
                            vision_content.append({"type": "text", "text": excel_text})
                        except json.JSONDecodeError:
                            continue
                    elif content.startswith("TEXT_DATA:"):
                        text_data = content[len("TEXT_DATA:"):]
                        batch_text += f"- {filename} (Text file)\n"
                        vision_content.append({
                            "type": "text",
                            "text": f"File: {filename}\nPath: {file_path}\nType: Text file\n\n{text_data}"
                        })
                    else:
                        # PDF image
                        image_base64 = content
                        batch_text += f"- {filename}, Page {page_num}\n"
                        vision_content.append({
                            "type": "text",
                            "text": f"File: {filename}\nPath: {file_path}\nPage: {page_num}\n\nReview this page against the Checklist above."
                        })
                        vision_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_base64}"}
                        })
                
                # Prepend knowledge and batch info
                vision_content.insert(0, {"type": "text", "text": batch_text})
                
                # Call GPT_10
                response = api_client.create_response(
                    system_prompt=GPT_10_CONFIG.get('instructions', ''),
                    user_input=vision_content,
                    model=GPT_10_CONFIG.get('model', 'gpt-5.1'),
                    temperature=GPT_10_CONFIG.get('temperature', 0.7)
                )
                
                all_findings.append({
                    'batch': batch_idx + 1,
                    'response': response
                })
                
            except Exception as e:
                logger.error(f"[PHASE 4] Checklist Review: Error in batch {batch_idx + 1}: {e}")
                continue
        
        if all_findings:
            return "\n\n".join([
                f"Batch {f['batch']}:\n{f['response']}"
                for f in all_findings
            ])
        return None
    
    def _build_final_result(self, job_state: JobState) -> Dict:
        """Build final result dictionary."""
        # Parse issues from discipline results (markdown tables) instead of merged output
        merged_issues = []
        
        if job_state.review_results:
            discipline_results = job_state.review_results.get('discipline_results', {})
            
            # Parse markdown tables from each discipline
            for discipline_name, discipline_output in discipline_results.items():
                # Extract markdown table rows
                # Pattern: | Discipline | Reference | Observation |
                # Skip header row (first match is usually the header)
                table_pattern = r'\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|'
                matches = re.findall(table_pattern, discipline_output)
                
                # Skip first match if it looks like a header (contains "Discipline", "Reference", etc.)
                for match in matches:
                    table_discipline, reference, observation = match
                    
                    # Skip header rows
                    if any(header_word in table_discipline.lower() for header_word in ['discipline', 'reference', 'observation', 'item']):
                        continue
                    
                    # Skip rows with empty or placeholder observations
                    clean_observation_check = observation.strip()
                    if not clean_observation_check or clean_observation_check == '---' or len(clean_observation_check) < 3:
                        continue
                    
                    # Skip rows with empty references
                    clean_reference_check = reference.strip()
                    if not clean_reference_check or clean_reference_check == '---':
                        continue
                    
                    # Clean discipline: use the discipline from the key (not from table)
                    clean_discipline = discipline_name.strip()
                    # Remove any markdown formatting that might be in discipline name
                    clean_discipline = re.sub(r'\*\*|\*|`|\[|\]', '', clean_discipline)
                    
                    # Clean reference: extract just the file/sheet name
                    clean_reference = reference.strip()
                    # Remove markdown formatting
                    clean_reference = re.sub(r'\*\*|\*|`', '', clean_reference)
                    # Remove "File:" prefix if present (case-insensitive)
                    clean_reference = re.sub(r'^File:\s*', '', clean_reference, flags=re.IGNORECASE).strip()
                    
                    # Extract file/sheet name (before comma, dash, or "Page")
                    # Try to extract sheet name first
                    sheet_match = re.search(r'Sheet\s+([^,–\-]+)', clean_reference, re.IGNORECASE)
                    if sheet_match:
                        clean_reference = sheet_match.group(1).strip()
                    else:
                        # Try to extract filename from path (PDF files)
                        filename_match = re.search(r'([^/\\]+\.(?:pdf|PDF|xlsx|XLSX|xls|XLS))', clean_reference)
                        if filename_match:
                            clean_reference = filename_match.group(1)
                        else:
                            # Fallback: use first part before comma, dash, or "Page"
                            clean_reference = re.split(r'[,–\-]|Page', clean_reference, maxsplit=1)[0].strip()
                    
                    # Clean observation: remove markdown formatting
                    clean_observation = observation.strip()
                    clean_observation = re.sub(r'\*\*|\*|`', '', clean_observation)
                    
                    merged_issues.append({
                        'discipline': clean_discipline,
                        'reference': clean_reference,
                        'observation': clean_observation,
                        'severity': ''  # Will be populated from merged_output
                    })
        
        # Try to extract severity from merged_output
        merged_output_text = job_state.review_results.get('merged_output', '') if job_state.review_results else ''
        
        if merged_output_text:
            # Try to match issues with severity from merged_output
            # Look for patterns like "[Critical]", "(Severity: High)", "Critical:", etc.
            for issue in merged_issues:
                # Try multiple patterns to find severity
                severity_patterns = [
                    # Pattern: [Critical] or [High] etc. near the issue
                    r'\[(Critical|High|Medium|Low)\][^\n]*?' + re.escape(issue['discipline']) + r'[^\n]*?' + re.escape(issue['reference']),
                    # Pattern: Critical: or High: etc.
                    r'(Critical|High|Medium|Low):\s*[^\n]*?' + re.escape(issue['discipline']) + r'[^\n]*?' + re.escape(issue['reference']),
                    # Pattern: (Severity: Critical) or (Severity: High) etc.
                    r'\(Severity:\s*(Critical|High|Medium|Low)\)[^\n]*?' + re.escape(issue['discipline']) + r'[^\n]*?' + re.escape(issue['reference']),
                    # Pattern: Numbered list with severity in brackets
                    r'\d+\.\s*\[(Critical|High|Medium|Low)\][^\n]*?' + re.escape(issue['discipline']) + r'[^\n]*?' + re.escape(issue['reference']),
                ]
                
                for pattern in severity_patterns:
                    match = re.search(pattern, merged_output_text, re.IGNORECASE | re.DOTALL)
                    if match:
                        issue['severity'] = match.group(1).capitalize()
                        break
                
                # If still no severity found, try to find it by observation text
                if not issue['severity']:
                    # Look for severity near the observation text
                    obs_patterns = [
                        r'\[(Critical|High|Medium|Low)\][^\n]*?' + re.escape(issue['observation'][:50]),
                        r'(Critical|High|Medium|Low):\s*[^\n]*?' + re.escape(issue['observation'][:50]),
                    ]
                    for pattern in obs_patterns:
                        match = re.search(pattern, merged_output_text, re.IGNORECASE | re.DOTALL)
                        if match:
                            issue['severity'] = match.group(1).capitalize()
                            break
        
        # Sort issues by discipline (primary), then by severity (secondary)
        severity_order = {'Critical': 0, 'High': 1, 'Medium': 2, 'Low': 3}
        merged_issues.sort(key=lambda x: (
            x.get('discipline', '').strip(),  # Primary: discipline (alphabetically)
            severity_order.get(x.get('severity', '').capitalize(), 99)  # Secondary: severity
        ))
        
        # Re-enumerate item numbers sequentially (1, 2, 3, ...)
        for idx, issue in enumerate(merged_issues, start=1):
            issue['item_no'] = idx
        
        # Build final output text
        final_output = ""
        if job_state.review_results:
            if job_state.review_results.get('merged_output'):
                final_output = job_state.review_results['merged_output']
            else:
                # Fallback: combine discipline results
                discipline_results = job_state.review_results.get('discipline_results', {})
                final_output = "\n\n".join([
                    f"=== {d} ===\n{r}"
                    for d, r in discipline_results.items()
                ])
        
        return {
            "success": job_state.phase == Phase.COMPLETE,
            "total_issues": len(merged_issues),
            "discipline_results": job_state.review_results.get('discipline_results', {}) if job_state.review_results else {},
            "discipline_errors": job_state.review_results.get('discipline_errors', {}) if job_state.review_results else {},
            "merged_issues": merged_issues,
            "final_output": final_output or "Review complete",
            "files_listed": len(job_state.file_paths) if job_state.file_paths else 0,
            "files_processed": len([f for f in job_state.files.values() if f.get('state') == FileState.DONE.value]) if job_state.files else 0
        }
