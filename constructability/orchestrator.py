"""
Constructability Orchestrator API endpoint.
"""
from flask import Flask, request, jsonify, redirect
from urllib.parse import urlencode, quote
import requests
import sys
import os
import re
import fnmatch
from collections import deque
import uuid
import threading
from datetime import datetime
import logging

# Add parent directory to path for shared imports
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Add current directory to path for local imports
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from shared.config import SharedConfig
from workflow import ConstructabilityWorkflow

# CSV Template configuration
CSV_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'static',
    'result_template.csv'
)

# Store job-specific log handlers for cleanup
job_log_handlers = {}
job_log_handlers_lock = threading.Lock()

# Configure basic session logging (app-level only)
def setup_logging():
    """Configure basic session logging for app-level events."""
    # Check if logging is already configured (avoid duplicate handlers)
    root_logger = logging.getLogger()
    if root_logger.handlers:
        # Logging already configured, just return the logger
        return logging.getLogger(__name__)
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler (for IIS stdout) - app-level only
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    
    # Root logger
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    
    # Flask logger - reduce verbosity
    flask_logger = logging.getLogger('werkzeug')
    flask_logger.setLevel(logging.WARNING)
    
    # Get module logger
    module_logger = logging.getLogger(__name__)
    module_logger.info("Session logging initialized (app-level only)")
    
    return module_logger

def setup_job_logging(job_id: str):
    """Create a per-review log file for a specific job."""
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Create logs subdirectory
    log_dir = os.path.join(script_dir, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    # Optional: Clean up old log files (older than 30 days)
    try:
        import glob
        import time
        cutoff_time = time.time() - (30 * 24 * 60 * 60)  # 30 days ago
        for log_file in glob.glob(os.path.join(log_dir, 'review_*.log')):
            if os.path.getmtime(log_file) < cutoff_time:
                os.remove(log_file)
    except Exception:
        # Don't fail if cleanup doesn't work
        pass
    
    # Create timestamped filename with job_id
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = os.path.join(log_dir, f'review_{job_id}_{timestamp}.log')
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # File handler for this job
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    
    # Add handler to root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    
    # Store handler for cleanup later
    with job_log_handlers_lock:
        job_log_handlers[job_id] = file_handler
    
    # Log that job logging has started
    logger = logging.getLogger(__name__)
    logger.info(f"Job {job_id} logging initialized - Log file: {log_file}")
    
    return log_file

def cleanup_job_logging(job_id: str):
    """Remove job-specific log handler when job completes."""
    with job_log_handlers_lock:
        if job_id in job_log_handlers:
            handler = job_log_handlers[job_id]
            root_logger = logging.getLogger()
            root_logger.removeHandler(handler)
            handler.close()
            del job_log_handlers[job_id]

# Setup logging before creating app
logger = setup_logging()

app = Flask(__name__)
workflow = ConstructabilityWorkflow()

# In-memory job storage
# Structure: {job_id: {status, progress, result, error, created_at, updated_at}}
jobs = {}
jobs_lock = threading.Lock()


def _create_job() -> str:
    """Create a new job and return its ID."""
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            'status': 'queued',
            'progress': {
                'files_fetched': 0,
                'total_files': 0,
                'files_preprocessed': 0,
                'current_stage': 'Initializing'
            },
            'result': None,
            'error': None,
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat()
        }
    return job_id


def _update_job_progress(job_id: str, **updates):
    """Update job progress."""
    with jobs_lock:
        if job_id in jobs:
            if 'progress' in updates:
                jobs[job_id]['progress'].update(updates['progress'])
                stage = updates['progress'].get('current_stage', 'N/A')
                logger.info(f"Job {job_id}: {stage}")
                del updates['progress']
            if 'status' in updates:
                logger.info(f"Job {job_id} status changed to: {updates['status']}")
            jobs[job_id].update(updates)
            jobs[job_id]['updated_at'] = datetime.now().isoformat()


def _get_job(job_id: str):
    """Get job by ID."""
    with jobs_lock:
        return jobs.get(job_id)


def _delete_job(job_id: str):
    """Delete a job."""
    with jobs_lock:
        if job_id in jobs:
            del jobs[job_id]


def _cleanup_old_jobs():
    """Remove jobs older than 1 hour."""
    now = datetime.now()
    with jobs_lock:
        to_delete = []
        for job_id, job_data in jobs.items():
            created_at = datetime.fromisoformat(job_data['created_at'])
            if (now - created_at).total_seconds() > 3600:  # 1 hour
                to_delete.append(job_id)
        for job_id in to_delete:
            del jobs[job_id]
        if to_delete:
            logger.info(f"Deleted {len(to_delete)} old jobs")


def _should_exclude_path(file_path: str, excluded_paths: list) -> bool:
    """
    Check if a file path should be excluded based on excluded_paths.
    Handles both file paths (exact match) and folder paths (prefix match).
    
    Args:
        file_path: The file path to check (e.g., "/Shared/Projects/X/drawing.pdf")
        excluded_paths: List of file/folder paths to exclude from GPT
                         (e.g., ["/Shared/Projects/X/Archive", "/Shared/Projects/X/old_drawing.pdf"])
    
    Returns:
        True if the path should be excluded, False otherwise
    """
    if not excluded_paths:
        return False
    
    # Normalize paths for comparison (ensure they start with /)
    normalized_file_path = file_path if file_path.startswith('/') else '/' + file_path
    
    for excluded_path in excluded_paths:
        if not excluded_path:  # Skip empty strings
            continue
            
        # Normalize excluded path
        normalized_excluded = excluded_path if excluded_path.startswith('/') else '/' + excluded_path
        
        # Case 1: Exact match (file exclusion)
        if normalized_file_path == normalized_excluded:
            return True
        
        # Case 2: File is inside an excluded folder
        # Ensure folder path ends with / for proper prefix matching
        excluded_with_slash = normalized_excluded if normalized_excluded.endswith('/') else normalized_excluded + '/'
        if normalized_file_path.startswith(excluded_with_slash):
            return True
        
        # Case 3: Handle case where excluded path doesn't have trailing slash
        # but still represents a folder (e.g., "/Shared/Archive" should exclude "/Shared/Archive/file.pdf")
        if normalized_file_path.startswith(normalized_excluded + '/'):
            return True
    
    return False


def _process_constructability_review_async(job_id: str, egnyte_token: str, data: dict):
    """Background function to process constructability review asynchronously."""
    try:
        # Setup per-review logging for this job
        log_file = setup_job_logging(job_id)
        logger.info(f"Starting constructability review job {job_id} - Log file: {log_file}")
        
        _update_job_progress(job_id, status='processing', progress={'current_stage': 'Fetching files from Egnyte'})
        
        # Extract parameters
        selected_disciplines = data['selected_disciplines']
        excluded_paths = data.get('excluded_paths', [])  # Can be files or folders from GPT
        
        if excluded_paths:
            logger.info(f"Excluding {len(excluded_paths)} path(s): {excluded_paths}")
        
        # Determine file paths to fetch (same logic as sync endpoint)
        file_paths = []
        
        if 'file_paths' in data and data['file_paths']:
            file_paths = data['file_paths']
            expanded_file_paths = []
            for path in file_paths:
                # Skip if this path itself is excluded
                if _should_exclude_path(path, excluded_paths):
                    logger.info(f"Skipping excluded path: {path}")
                    continue
                
                if _is_egnyte_folder(egnyte_token, path):
                    try:
                        folder_files = _list_files_recursive(
                            egnyte_token=egnyte_token,
                            folder_path=path,
                            include_globs=data.get('include_globs', []),
                            exclude_globs=data.get('exclude_globs', [])
                        )
                        # Filter out excluded paths from folder files
                        for f in folder_files:
                            if not _should_exclude_path(f['path'], excluded_paths):
                                expanded_file_paths.append(f['path'])
                    except Exception as e:
                        logger.debug(f"Error expanding folder '{path}': {e}")
                else:
                    expanded_file_paths.append(path)
            file_paths = expanded_file_paths
        
        elif 'egnyte_folder_path' in data and data['egnyte_folder_path']:
            folder_path = data['egnyte_folder_path']
            if not folder_path.startswith('/'):
                folder_path = '/' + folder_path
            
            # Skip if the folder itself is excluded
            if _should_exclude_path(folder_path, excluded_paths):
                _update_job_progress(job_id, status='error', error='The specified folder is excluded from review')
                return
            
            file_list = _list_files_recursive(
                egnyte_token=egnyte_token,
                folder_path=folder_path,
                include_globs=data.get('include_globs', []),
                exclude_globs=data.get('exclude_globs', [])
            )
            # Filter out excluded paths
            file_paths = [f['path'] for f in file_list if not _should_exclude_path(f['path'], excluded_paths)]
        
        if len(file_paths) == 0:
            _update_job_progress(job_id, status='error', error='No files found to process after applying exclusions')
            return
        
        # Update progress: files found
        _update_job_progress(job_id, progress={
            'total_files': len(file_paths),
            'files_fetched': len(file_paths),  # All paths are "found"
            'current_stage': f'Found {len(file_paths)} files - Phase 1 will extract metadata on-demand (no base64)'
        })
        
        logger.info(f"[PHASE 1] Starting with {len(file_paths)} file paths - will fetch metadata on-demand (no base64 encoding)")
        logger.info(f"Starting workflow execution for job {job_id} with {len(file_paths)} file paths and {len(selected_disciplines)} disciplines")
        
        # Create progress callback
        def progress_callback(progress_update):
            _update_job_progress(job_id, progress=progress_update)
        
        # Execute workflow with progress callback
        try:
            result = workflow.execute_constructability_review(
                file_paths=file_paths,
                selected_disciplines=selected_disciplines,
                egnyte_token=egnyte_token,
                progress_callback=progress_callback
            )
            logger.info(f"Workflow execution completed for job {job_id}")
            
            # Log results
            logger.info("="*80)
            logger.info("CONSTRUCTABILITY REVIEW RESULTS")
            logger.info("="*80)
            logger.info(f"Total Issues Found: {result.get('total_issues', 0)}")
            logger.info(f"Files Processed: {result.get('files_processed', 0)}/{result.get('files_listed', 0)}")
            logger.info("--- MERGED OUTPUT ---")
            logger.info(result.get('final_output', 'No output generated'))
            logger.info("--- DISCIPLINE RESULTS ---")
            for discipline, output in result.get('discipline_results', {}).items():
                logger.info(f"[{discipline}]")
                logger.info(output[:500] + "..." if len(output) > 500 else output)
            if result.get('discipline_errors'):
                logger.info("--- ERRORS ---")
                for discipline, error in result.get('discipline_errors', {}).items():
                    logger.error(f"[{discipline}]: {error}")
            if result.get('merged_issues'):
                logger.info(f"--- PARSED ISSUES ({len(result.get('merged_issues', []))} items) ---")
                for issue in result.get('merged_issues', [])[:10]:  # Show first 10
                    logger.info(f"{issue.get('item_no', '?')}. [{issue.get('discipline', 'Unknown')}] {issue.get('reference', 'N/A')}: {issue.get('observation', 'N/A')[:100]}")
                if len(result.get('merged_issues', [])) > 10:
                    logger.info(f"... and {len(result.get('merged_issues', [])) - 10} more issues")
            logger.info("="*80)
            
        except Exception as e:
            import traceback
            error_msg = f"Workflow execution failed: {str(e)}"
            error_traceback = traceback.format_exc()
            logger.error(f"{error_msg}")
            logger.error(f"Traceback:\n{error_traceback}")
            _update_job_progress(job_id, status='error', error=error_msg)
            
            # Cleanup job-specific logging
            cleanup_job_logging(job_id)
            
            _delete_job(job_id)
            return
        
        # Write CSV using template-based approach
        try:
            _update_job_progress(job_id, progress={'current_stage': 'Writing CSV file from template'})
            
            # Get merged_issues (may be empty list or None)
            merged_issues = result.get('merged_issues', [])
            if not merged_issues:
                logger.warning("No merged_issues found, writing empty CSV")
            
            # Get CSV output parameters from request
            csv_output_folder = data.get('csv_output_folder')
            csv_output_filename = data.get('csv_output_filename')
            
            # If both are provided, create CSV and upload to Egnyte
            if csv_output_folder and csv_output_filename:
                # Ensure folder path starts with /
                if not csv_output_folder.startswith('/'):
                    csv_output_folder = '/' + csv_output_folder
                
                # Generate CSV content with provided filename
                csv_content, csv_filename = _write_issues_to_csv(merged_issues or [], csv_output_filename)
                
                # Build full Egnyte path
                egnyte_csv_path = f"{csv_output_folder.rstrip('/')}/{csv_filename}"
                
                logger.info(f"Uploading CSV to Egnyte: {egnyte_csv_path}")
                
                # Upload to Egnyte (folder already verified to exist in start endpoint)
                upload_success = _upload_file_to_egnyte(egnyte_token, egnyte_csv_path, csv_content)
                
                if upload_success:
                    result['csv_output_path'] = egnyte_csv_path
                    result['csv_written'] = True
                    result['csv_uploaded_to_egnyte'] = True
                    logger.info(f"Successfully uploaded CSV to Egnyte: {egnyte_csv_path}")
                else:
                    result['csv_error'] = f"Failed to upload CSV to Egnyte: {egnyte_csv_path}"
                    result['csv_written'] = False
                    logger.error(f"Failed to upload CSV to Egnyte: {egnyte_csv_path}")
            else:
                # No CSV output specified - skip CSV generation
                logger.info("No CSV output folder/filename provided, skipping CSV generation")
                result['csv_written'] = False
                result['csv_output_path'] = None
            
        except Exception as e:
            result['csv_error'] = str(e)
            logger.error(f"Error writing CSV from template: {e}")
        
        # Mark job as complete
        _update_job_progress(job_id, status='complete', result=result, progress={'current_stage': 'Complete'})
        logger.info(f"Job {job_id} completed successfully")
        
        # Cleanup job-specific logging
        cleanup_job_logging(job_id)
        
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_traceback = traceback.format_exc()
        logger.error(f"Job {job_id} failed: {error_msg}")
        logger.error(f"Traceback:\n{error_traceback}")
        _update_job_progress(job_id, status='error', error=error_msg)
        
        # Cleanup job-specific logging
        cleanup_job_logging(job_id)
        
        # Delete failed job
        _delete_job(job_id)


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({'status': 'healthy', 'orchestrator': 'constructability'})


@app.route('/routes', methods=['GET'])
def list_routes():
    """List all available routes (for debugging)."""
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append({
            'endpoint': rule.endpoint,
            'methods': list(rule.methods),
            'path': str(rule)
        })
    return jsonify({'routes': routes})


@app.route('/oauth/authorize', methods=['GET'])
def oauth_authorize():
    """
    OAuth authorize proxy endpoint.
    Redirects the user to Egnyte's authorization page.
    ChatGPT will call this to initiate OAuth flow.
    """
    logger.debug("="*80)
    logger.debug("/oauth/authorize called")
    logger.debug(f"Request URL: {request.url}")
    logger.debug(f"Request headers: {dict(request.headers)}")
    
    if not SharedConfig.EGNYTE_CLIENT_ID:
        logger.error("EGNYTE_CLIENT_ID not configured")
        return jsonify({
            'error': 'Egnyte OAuth not configured. EGNYTE_CLIENT_ID is required.'
        }), 500
    
    # Get all query parameters from ChatGPT
    params = request.args.to_dict()
    logger.debug(f"Parameters from ChatGPT: {params}")
    
    # Override client_id with our Egnyte app's client ID
    original_client_id = params.get('client_id', 'NOT_PROVIDED')
    params['client_id'] = SharedConfig.EGNYTE_CLIENT_ID
    logger.debug(f"Original client_id from ChatGPT: {original_client_id}")
    logger.debug(f"Using Egnyte client_id: {SharedConfig.EGNYTE_CLIENT_ID}")
    
    # Check redirect_uri specifically
    redirect_uri = params.get('redirect_uri', 'NOT_PROVIDED')
    logger.debug(f"*** REDIRECT_URI FROM CHATGPT: {redirect_uri} ***")
    logger.debug("This redirect_uri MUST be registered in your Egnyte OAuth app!")
    
    # Build Egnyte authorization URL
    egnyte_auth_url = SharedConfig.EGNYTE_AUTHORIZATION_URL
    logger.debug(f"Egnyte authorization URL: {egnyte_auth_url}")
    
    # Validate Egnyte auth URL
    if not egnyte_auth_url:
        logger.error("EGNYTE_AUTHORIZATION_URL not configured")
        return jsonify({
            'error': 'Egnyte OAuth configuration error. Check EGNYTE_AUTHORIZATION_URL.'
        }), 500
    
    # URL encode parameters and build redirect URL
    query_string = urlencode(params, doseq=True)
    redirect_url = f"{egnyte_auth_url}?{query_string}"
    logger.debug(f"Redirecting user to Egnyte with full URL (truncated): {redirect_url[:200]}...")
    logger.debug("="*80)
    
    # Redirect user to Egnyte
    return redirect(redirect_url)


@app.route('/oauth/token', methods=['POST'])
def oauth_token():
    """
    OAuth token proxy endpoint.
    Exchanges an authorization code for tokens via Egnyte.
    ChatGPT will call this to complete OAuth flow.
    """
    logger.debug("="*80)
    logger.debug("/oauth/token called")
    logger.debug(f"Request method: {request.method}")
    logger.debug(f"Content-Type: {request.content_type}")
    logger.debug(f"Request headers: {dict(request.headers)}")
    
    if not SharedConfig.EGNYTE_CLIENT_ID or not SharedConfig.EGNYTE_CLIENT_SECRET:
        logger.error("EGNYTE_CLIENT_ID or EGNYTE_CLIENT_SECRET not configured")
        return jsonify({
            'error': 'Egnyte OAuth not configured. EGNYTE_CLIENT_ID and EGNYTE_CLIENT_SECRET are required.'
        }), 500
    
    # Get request data - OAuth token requests can use form-urlencoded or JSON
    if request.content_type and 'application/json' in request.content_type:
        data = request.get_json() or {}
        logger.debug("Parsed JSON data from ChatGPT")
    else:
        # Form-urlencoded (standard OAuth format)
        data = request.form.to_dict() if request.form else {}
        logger.debug("Parsed form-urlencoded data from ChatGPT")
    
    logger.debug(f"Data from ChatGPT: {data}")
    
    if not data:
        logger.error("No data provided in request")
        return jsonify({
            'error': 'Invalid request format',
            'error_description': 'No data provided in request'
        }), 400
    
    # Prepare token request to Egnyte
    token_data = {
        'grant_type': data.get('grant_type', 'authorization_code'),
        'client_id': SharedConfig.EGNYTE_CLIENT_ID,
        'client_secret': SharedConfig.EGNYTE_CLIENT_SECRET,
    }
    
    # Add grant-specific parameters
    if token_data['grant_type'] == 'authorization_code':
        token_data['code'] = data.get('code')
        token_data['redirect_uri'] = data.get('redirect_uri')
        logger.debug(f"*** REDIRECT_URI BEING SENT TO EGNYTE: {token_data.get('redirect_uri')} ***")
        logger.debug(f"Authorization code: {token_data.get('code', 'NOT_PROVIDED')[:20]}...")
    elif token_data['grant_type'] == 'refresh_token':
        token_data['refresh_token'] = data.get('refresh_token')
        logger.debug("Using refresh_token grant")
    
    # Add scope if provided
    if 'scope' in data:
        token_data['scope'] = data['scope']
    
    logger.debug(f"Token data being sent to Egnyte (client_secret hidden): { {k: ('***HIDDEN***' if k == 'client_secret' else v) for k, v in token_data.items()} }")
    
    try:
        # Exchange code for token with Egnyte
        egnyte_token_url = SharedConfig.EGNYTE_TOKEN_URL
        logger.debug(f"Sending POST request to Egnyte: {egnyte_token_url}")
        response = requests.post(egnyte_token_url, data=token_data)
        
        logger.debug(f"Egnyte response status: {response.status_code}")
        logger.debug(f"Egnyte response headers: {dict(response.headers)}")
        
        if response.status_code != 200:
            logger.error("Egnyte returned non-200 status")
            try:
                error_body = response.json()
                logger.error(f"Egnyte error response: {error_body}")
            except:
                logger.error(f"Egnyte error response (text): {response.text}")
        
        response.raise_for_status()
        
        # Return the token response to ChatGPT (unchanged)
        token_response = response.json()
        logger.debug("Successfully exchanged code for token")
        logger.debug(f"Token response keys: {list(token_response.keys())}")
        logger.debug("="*80)
        return jsonify(token_response), response.status_code
        
    except requests.exceptions.RequestException as e:
        error_detail = str(e)
        logger.error(f"RequestException occurred: {error_detail}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Error response status: {e.response.status_code}")
            try:
                error_response = e.response.json()
                logger.error(f"Error response body: {error_response}")
                error_detail = f"{error_detail}. Response: {error_response}"
            except:
                error_text = e.response.text
                logger.error(f"Error response text: {error_text}")
                error_detail = f"{error_detail}. Status: {e.response.status_code}. Text: {error_text}"
        
        logger.debug("="*80)
        return jsonify({
            'error': 'Failed to exchange authorization code for token',
            'error_description': error_detail
        }), 500


def _extract_egnyte_token():
    """Extract Egnyte token from Authorization header."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None
    token = auth_header.replace('Bearer ', '').strip()
    return token if token else None


def _is_egnyte_folder(egnyte_token: str, path: str) -> bool:
    """
    Check if a path is a folder by attempting to list it.
    Returns True if it's a folder, False if it's a file or doesn't exist.
    """
    try:
        egnyte_base_url = SharedConfig.EGNYTE_BASE_URL.rstrip('/')
        encoded_path = quote(path, safe='/')
        list_url = f"{egnyte_base_url}/pubapi/v1/fs{encoded_path}"
        
        headers = {
            'Authorization': f'Bearer {egnyte_token}',
            'Content-Type': 'application/json'
        }
        
        response = requests.get(list_url, headers=headers, timeout=5)
        # If we get a 200 response, it's a folder (listing succeeded)
        if response.status_code == 200:
            return True
        # If we get 404, it might be a file or doesn't exist
        return False
    except Exception as e:
        # If listing fails, assume it's not a folder
        logger.debug(f"Could not determine if '{path}' is folder: {e}")
        return False


def _fetch_file_from_egnyte(egnyte_token: str, file_path: str, max_retries: int = 3) -> bytes:
    """Fetch file content from Egnyte API with retry logic for rate limiting."""
    import time
    
    egnyte_base_url = SharedConfig.EGNYTE_BASE_URL.rstrip('/')
    # URL encode the path to handle special characters (spaces, %, etc.)
    # Egnyte API expects the path to be URL-encoded
    encoded_path = quote(file_path, safe='/')  # Keep '/' unencoded, encode everything else
    file_url = f"{egnyte_base_url}/pubapi/v1/fs-content{encoded_path}"
    
    logger.debug(f"Fetching file: {file_path}")
    logger.debug(f"Encoded URL: {file_url}")
    
    headers = {
        'Authorization': f'Bearer {egnyte_token}',
    }
    
    for attempt in range(max_retries):
        try:
            response = requests.get(file_url, headers=headers)
            
            # Handle rate limiting (429) with exponential backoff
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 2 ** attempt))
                wait_time = min(retry_after, 30)  # Cap at 30 seconds
                logger.warning(f"429 Too Many Requests for {file_path}. Waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                time.sleep(wait_time)
                continue
            
            response.raise_for_status()
            return response.content
            
        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 429:
                # Already handled above, but catch here too
                if attempt < max_retries - 1:
                    retry_after = int(e.response.headers.get('Retry-After', 2 ** attempt))
                    wait_time = min(retry_after, 30)
                    logger.warning(f"429 Too Many Requests for {file_path}. Waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                    time.sleep(wait_time)
                    continue
            raise
    
    # If we get here, all retries failed
    raise requests.exceptions.HTTPError(f"Failed to fetch {file_path} after {max_retries} retries due to rate limiting")


def _upload_file_to_egnyte(egnyte_token: str, file_path: str, file_content: bytes) -> bool:
    """Upload file content to Egnyte API. Creates parent folder if it doesn't exist."""
    try:
        # Ensure parent folder exists
        parent_folder = '/'.join(file_path.split('/')[:-1])
        if parent_folder:
            _ensure_egnyte_folder_exists(egnyte_token, parent_folder)
        
        egnyte_base_url = SharedConfig.EGNYTE_BASE_URL.rstrip('/')
        # URL encode the path
        encoded_path = quote(file_path, safe='/')
        file_url = f"{egnyte_base_url}/pubapi/v1/fs-content{encoded_path}"
        
        logger.debug(f"Uploading file to: {file_path}")
        logger.debug(f"Encoded URL: {file_url}")
        
        headers = {
            'Authorization': f'Bearer {egnyte_token}',
            'Content-Type': 'application/octet-stream'
        }
        
        response = requests.post(file_url, headers=headers, data=file_content)
        
        # Check for 404 - might be case sensitivity issue
        if response.status_code == 404:
            parent_folder = '/'.join(file_path.split('/')[:-1])
            logger.error(f"Failed to upload file to {file_path}: 404 Not Found")
            return False
        
        response.raise_for_status()
        logger.debug(f"Successfully uploaded file to {file_path}")
        return True
    except requests.exceptions.HTTPError as e:
        if e.response and e.response.status_code == 404:
            parent_folder = '/'.join(file_path.split('/')[:-1])
            logger.error(f"Failed to upload file to {file_path}: 404 Not Found")
        else:
            logger.error(f"Failed to upload file to {file_path}: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to upload file to {file_path}: {e}")
        return False


def _create_test_csv_and_upload(egnyte_token: str, csv_output_folder: str, csv_output_filename: str) -> tuple[bool, str]:
    """
    Create a simple test CSV with "test" content and upload to Egnyte.
    
    Args:
        egnyte_token: Egnyte OAuth token
        csv_output_folder: Egnyte folder path (e.g., "/Shared")
        csv_output_filename: CSV filename (e.g., "test.csv" or "test")
    
    Returns:
        tuple: (success: bool, message: str)
    """
    import csv
    import io
    
    try:
        # Ensure filename has .csv extension
        if not csv_output_filename.lower().endswith('.csv'):
            csv_output_filename = csv_output_filename + '.csv'
        
        # Create a simple CSV with "test" content
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header row
        writer.writerow(['Item No.', 'Discipline', 'Severity', 'Reference', 'Observation'])
        
        # Write test row
        writer.writerow(['1', 'Test', 'Test', 'Test', 'This is a test CSV file'])
        
        # Convert to bytes
        csv_bytes = output.getvalue().encode('utf-8-sig')  # UTF-8 with BOM for Excel compatibility
        
        # Ensure folder path starts with /
        if not csv_output_folder.startswith('/'):
            csv_output_folder = '/' + csv_output_folder
        
        # Build full Egnyte path
        egnyte_csv_path = f"{csv_output_folder.rstrip('/')}/{csv_output_filename}"
        
        logger.info(f"Creating test CSV: {csv_output_filename}")
        logger.info(f"Ensuring destination folder exists: {csv_output_folder}")
        
        # Ensure destination folder exists before attempting upload
        folder_exists = _ensure_egnyte_folder_exists(egnyte_token, csv_output_folder)
        if not folder_exists:
            return False, f"Failed to create or verify destination folder: {csv_output_folder}"
        
        logger.info(f"Destination folder verified/created: {csv_output_folder}")
        logger.info(f"Uploading to Egnyte: {egnyte_csv_path}")
        
        # Upload to Egnyte
        upload_success = _upload_file_to_egnyte(egnyte_token, egnyte_csv_path, csv_bytes)
        
        if upload_success:
            return True, f"Test CSV successfully uploaded to {egnyte_csv_path}"
        else:
            return False, f"Failed to upload CSV to Egnyte: {egnyte_csv_path}"
            
    except Exception as e:
        import traceback
        error_msg = f"Error creating/uploading test CSV: {str(e)}"
        logger.error(f"{error_msg}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        return False, error_msg


def _ensure_egnyte_folder_exists(egnyte_token: str, folder_path: str) -> bool:
    """Check if an Egnyte folder exists, create it if it doesn't."""
    try:
        egnyte_base_url = SharedConfig.EGNYTE_BASE_URL.rstrip('/')
        encoded_path = quote(folder_path, safe='/')
        folder_url = f"{egnyte_base_url}/pubapi/v1/fs{encoded_path}"
        
        headers = {
            'Authorization': f'Bearer {egnyte_token}',
            'Content-Type': 'application/json'
        }
        
        # Try to list the folder (will fail if it doesn't exist)
        response = requests.get(folder_url, headers=headers, timeout=5)
        if response.status_code == 200:
            return True  # Folder exists
        
        # Folder doesn't exist, try to create it
        if response.status_code == 404:
            logger.info(f"Folder does not exist, attempting to create: {folder_path}")
            
            # Create folder by making a POST request to /pubapi/v1/fs/{path}
            # According to Egnyte API: POST /pubapi/v1/fs/{Full Path to Folder}
            create_response = requests.post(
                folder_url,
                headers=headers,
                json={"action": "add_folder"},
                timeout=10
            )
            
            if create_response.status_code in [200, 201]:
                logger.info(f"Created folder: {folder_path}")
                return True
            elif create_response.status_code == 409:
                # Folder already exists (race condition)
                logger.info(f"Folder already exists: {folder_path}")
                return True
            else:
                error_text = create_response.text if hasattr(create_response, 'text') else str(create_response.status_code)
                logger.error(f"Could not create folder {folder_path}: {create_response.status_code} - {error_text}")
                # Try to create parent folders recursively if this fails
                return _create_egnyte_folder_recursive(egnyte_token, folder_path)
        
        # Other error
        logger.error(f"Error checking folder {folder_path}: {response.status_code}")
        return False
        
    except Exception as e:
        logger.error(f"Error ensuring folder exists {folder_path}: {e}")
        return False


def _create_egnyte_folder_recursive(egnyte_token: str, folder_path: str) -> bool:
    """Recursively create folder and all parent folders if they don't exist."""
    try:
        # Split path into components
        parts = folder_path.strip('/').split('/')
        if not parts or parts[0] == '':
            return False
        
        # Build path incrementally
        current_path = ''
        for part in parts:
            current_path = f"{current_path}/{part}" if current_path else f"/{part}"
            
            # Check if this level exists
            egnyte_base_url = SharedConfig.EGNYTE_BASE_URL.rstrip('/')
            encoded_path = quote(current_path, safe='/')
            check_url = f"{egnyte_base_url}/pubapi/v1/fs{encoded_path}"
            
            headers = {
                'Authorization': f'Bearer {egnyte_token}',
                'Content-Type': 'application/json'
            }
            
            check_response = requests.get(check_url, headers=headers, timeout=5)
            if check_response.status_code == 200:
                continue  # Folder exists, move to next level
            
            # Create this level
            create_response = requests.post(
                check_url,
                headers=headers,
                json={"action": "add_folder"},
                timeout=10
            )
            
            if create_response.status_code not in [200, 201, 409]:
                logger.error(f"Failed to create folder level: {current_path} ({create_response.status_code})")
                return False
        
        return True
        
    except Exception as e:
        logger.error(f"Error creating folder recursively {folder_path}: {e}")
        return False


def _write_issues_to_csv(merged_issues: list, output_filename: str) -> tuple[bytes, str]:
    """
    Load template CSV, append issues, and return (csv_bytes, filename).
    Template is located at: static/result_template.csv (relative to this file).
    
    Args:
        merged_issues: List of issue dictionaries
        output_filename: Required CSV filename (e.g., "results.csv" or "results")
    
    Returns:
        tuple: (csv_bytes: bytes, filename: str) - filename will have .csv extension added if missing
    """
    import csv
    import io
    
    # Get template directory
    template_dir = os.path.dirname(CSV_TEMPLATE_PATH)
    
    # Check if template exists
    if not os.path.exists(CSV_TEMPLATE_PATH):
        raise FileNotFoundError(f"Template file not found: {CSV_TEMPLATE_PATH}")
    
    # Read existing template
    existing_rows = []
    with open(CSV_TEMPLATE_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        existing_rows = list(reader)
    
    # Validate template has correct structure (5 columns: Item No., Discipline, Severity, Reference, Observation)
    if existing_rows and len(existing_rows[0]) != 5:
        raise ValueError(f"Template must have 5 columns. Found {len(existing_rows[0])} columns.")
    
    # Create output in memory
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write existing rows (preserve template)
    for row in existing_rows:
        writer.writerow(row)
    
    # Append new issue rows
    for issue in merged_issues:
        item_no = issue.get('item_no', '')
        discipline = issue.get('discipline', 'General')
        severity = issue.get('severity', '')
        reference = issue.get('reference', '')
        observation = issue.get('observation', '')
        
        # Clean discipline: remove any markdown formatting
        discipline = re.sub(r'\*\*|\*|`|\[|\]', '', discipline).strip()
        
        # Clean severity: remove any markdown formatting
        severity = re.sub(r'\*\*|\*|`|\[|\]', '', severity).strip()
        
        # Clean reference: ensure no markdown formatting (already cleaned in parsing, but double-check)
        reference = re.sub(r'\*\*|\*|`', '', reference).strip()
        reference = reference.replace('|', ' ').strip()
        
        # Clean observation: remove markdown formatting
        observation = re.sub(r'\*\*|\*|`', '', observation).strip()
        observation = observation.replace('|', ' ').strip()
        
        writer.writerow([item_no, discipline, severity, reference, observation])
    
    # Ensure filename has .csv extension
    if not output_filename.lower().endswith('.csv'):
        output_filename = output_filename + '.csv'
    
    # Convert to bytes
    csv_bytes = output.getvalue().encode('utf-8-sig')  # UTF-8 with BOM for Excel compatibility
    
    return csv_bytes, output_filename


def _list_files_recursive(egnyte_token: str, folder_path: str, include_globs=None, exclude_globs=None):
    """Recursively list all files in an Egnyte folder. No limits - processes all files."""
    
    all_files = []
    queue = deque([folder_path])
    visited = set()
    
    while queue:
        current_path = queue.popleft()
        if current_path in visited:
            continue
        visited.add(current_path)
        
        # List folder contents
        egnyte_base_url = SharedConfig.EGNYTE_BASE_URL.rstrip('/')
        # URL encode the path
        encoded_path = quote(current_path, safe='/')
        list_url = f"{egnyte_base_url}/pubapi/v1/fs{encoded_path}"
        logger.debug(f"Listing folder: {current_path}")
        logger.debug(f"Encoded URL: {list_url}")
        headers = {
            'Authorization': f'Bearer {egnyte_token}',
            'Content-Type': 'application/json'
        }
        
        try:
            response = requests.get(list_url, headers=headers)
            response.raise_for_status()
            folder_data = response.json()
            
            # Process folders (add to queue)
            for folder in folder_data.get('folders', []):
                folder_path = folder.get('path', '')
                if folder_path and folder_path not in visited:
                    queue.append(folder_path)
            
            # Process files
            for file_entry in folder_data.get('files', []):
                file_path = file_entry.get('path', '')
                file_size = file_entry.get('size', 0)
                last_modified = file_entry.get('last_modified', None)
                
                if not file_path:
                    continue
                
                # Apply include/exclude filters
                if include_globs:
                    if not any(fnmatch.fnmatch(file_path, pattern) for pattern in include_globs):
                        continue
                
                if exclude_globs:
                    if any(fnmatch.fnmatch(file_path, pattern) for pattern in exclude_globs):
                        continue
                
                # No limits - add all files that pass filters
                all_files.append({
                    'path': file_path,
                    'size': file_size,
                    'last_modified': last_modified
                })
                
        except requests.exceptions.RequestException as e:
            logger.warning(f"Could not list folder {current_path}: {e}")
            continue
    
    logger.debug(f"Listed {len(all_files)} files from folder {folder_path}")
    return all_files


@app.route('/egnyte/list', methods=['POST'])
def egnyte_list_files():
    """
    List files from an Egnyte folder using the Bearer token from Authorization header.
    """
    try:
        # Extract token from Authorization header
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({
                'error': 'Missing or invalid Authorization header',
                'error_description': 'Expected Authorization: Bearer <token>'
            }), 401
        
        egnyte_token = auth_header.replace('Bearer ', '').strip()

        if not egnyte_token:
            return jsonify({
                'error': 'Missing token in Authorization header'
            }), 401
        
        # Get request data
        data = request.get_json()
        if not data:
            return jsonify({
                'error': 'Missing request body'
            }), 400
        
        folder_path = data.get('folder_path')
        if not folder_path:
            return jsonify({
                'error': 'Missing required field: folder_path'
            }), 400
        
        # Normalize path (ensure it starts with /)
        if not folder_path.startswith('/'):
            folder_path = '/' + folder_path
        
        # Call Egnyte API to list folder contents
        egnyte_base_url = SharedConfig.EGNYTE_BASE_URL.rstrip('/')
        list_url = f"{egnyte_base_url}/pubapi/v1/fs{folder_path}"
        logger.debug(f"Listing folder: {list_url}")
        headers = {
            'Authorization': f'Bearer {egnyte_token}',
            'Content-Type': 'application/json'
        }
        
        response = requests.get(list_url, headers=headers)
        response.raise_for_status()
        folder_data = response.json()
        
        # Parse Egnyte response and format for our API
        # Egnyte returns 'folders' and 'files' arrays separately
        all_items = []
        
        # Process folders
        egnyte_folders = folder_data.get('folders', [])
        for folder in egnyte_folders:
            item = {
                'path': folder.get('path', ''),
                'name': folder.get('name', ''),
                'type': 'folder',
            }
            all_items.append(item)
        
        # Process files
        egnyte_files = folder_data.get('files', [])
        for file_entry in egnyte_files:
            item = {
                'path': file_entry.get('path', ''),
                'name': file_entry.get('name', ''),
                'type': 'file',
                'size_bytes': file_entry.get('size', None),
                'modified_utc': file_entry.get('last_modified', None),
                'mime_type': None  # Egnyte doesn't always provide this
            }
            all_items.append(item)
        
        # Apply filters if provided
        include_globs = data.get('include_globs', [])
        exclude_globs = data.get('exclude_globs', [])
        # Simple glob filtering (basic implementation)
        if include_globs or exclude_globs:
            filtered_items = []
            for item in all_items:
                item_path = item['path']
                
                # Check exclude patterns
                excluded = False
                for pattern in exclude_globs:
                    if fnmatch.fnmatch(item_path, pattern):
                        excluded = True
                        break
                if excluded:
                    continue
                
                # Check include patterns (if any provided)
                if include_globs:
                    included = False
                    for pattern in include_globs:
                        if fnmatch.fnmatch(item_path, pattern):
                            included = True
                            break
                    if not included:
                        continue
                
                filtered_items.append(item)
            all_items = filtered_items
        
        # No limits - return all items
        
        return jsonify({
            'folder_path': folder_path,
            'files': all_items
        })
        
    except requests.exceptions.RequestException as e:
        error_detail = str(e)
        status_code = 500
        if hasattr(e, 'response') and e.response is not None:
            status_code = e.response.status_code
            try:
                error_response = e.response.json()
                error_detail = f"{error_detail}. Response: {error_response}"
            except:
                error_detail = f"{error_detail}. Status: {e.response.status_code}"
        
        return jsonify({
            'error': 'Failed to list Egnyte folder',
            'error_description': error_detail
        }), status_code
        
    except Exception as e:
        return jsonify({
            'error': 'Internal server error',
            'error_description': str(e)
        }), 500


@app.route('/workflow/execute-constructability-review', methods=['POST'])
def execute_constructability_review():
    """
    Execute the full constructability review workflow.
    
    Backend fetches files from Egnyte using Bearer token from Authorization header.
    
    Expected JSON body:
    {
        "file_paths": ["/Shared/Projects/X/drawing1.pdf", "/Shared/Projects/X/spec.csv"],
        OR
        "egnyte_folder_path": "/Shared/Projects/X",
        "include_globs": ["**/*.pdf", "**/*.csv"],
        "exclude_globs": ["**/Archive/**"],
        "excluded_paths": ["/Shared/Projects/X/Archive", "/Shared/Projects/X/old_drawing.pdf"],  // NEW: Can be files or folders
        "selected_disciplines": ["Fire Protection", "Electrical", "Structural"]
    }
    """
    try:
        # Extract token from Authorization header
        egnyte_token = _extract_egnyte_token()
        if not egnyte_token:
            return jsonify({
                'error': 'Missing or invalid Authorization header',
                'error_description': 'Expected Authorization: Bearer <token>'
            }), 401
        
        data = request.get_json()
        if not data:
            return jsonify({
                'error': 'Missing request body'
            }), 400
        
        if 'selected_disciplines' not in data:
            return jsonify({
                'error': 'Missing required field: selected_disciplines'
            }), 400
        
        selected_disciplines = data['selected_disciplines']
        if not isinstance(selected_disciplines, list) or len(selected_disciplines) == 0:
            return jsonify({
                'error': 'selected_disciplines must be a non-empty array'
            }), 400
        
        excluded_paths = data.get('excluded_paths', [])  # Can be files or folders from GPT
        
        if excluded_paths:
            logger.info(f"Excluding {len(excluded_paths)} path(s): {excluded_paths}")
        
        # Determine file paths to fetch
        file_paths = []
        
        if 'file_paths' in data and data['file_paths']:
            # Explicit file paths provided
            file_paths = data['file_paths']
            logger.debug(f"Received file_paths: {len(file_paths)} items")
            logger.debug(f"First few paths: {file_paths[:5] if len(file_paths) > 5 else file_paths}")
            
            if not isinstance(file_paths, list) or len(file_paths) == 0:
                return jsonify({
                    'error': 'file_paths must be a non-empty array'
                }), 400
            
            # Check if any paths are folders and expand them recursively
            # Use Egnyte API to determine if path is folder or file
            expanded_file_paths = []
            for path in file_paths:
                # Skip if this path itself is excluded
                if _should_exclude_path(path, excluded_paths):
                    logger.info(f"Skipping excluded path: {path}")
                    continue
                
                # Check if path is a folder by attempting to list it
                if _is_egnyte_folder(egnyte_token, path):
                    # It's a folder - recursively list files in it
                    logger.debug(f"Path '{path}' is a folder, expanding recursively...")
                    try:
                        folder_files = _list_files_recursive(
                            egnyte_token=egnyte_token,
                            folder_path=path,
                            include_globs=data.get('include_globs', []),
                            exclude_globs=data.get('exclude_globs', [])
                        )
                        # Filter out excluded paths
                        for f in folder_files:
                            if not _should_exclude_path(f['path'], excluded_paths):
                                expanded_file_paths.append(f['path'])
                        logger.debug(f"Expanded folder '{path}' to {len(folder_files)} files, {len(expanded_file_paths)} after exclusions")
                    except Exception as e:
                        logger.debug(f"Error expanding folder '{path}': {e}")
                        # Don't add the folder path itself - it's not a file
                        logger.debug(f"Skipping folder '{path}' - expansion failed")
                else:
                    # It's a file (or doesn't exist) - add it to be fetched
                    expanded_file_paths.append(path)
            
            file_paths = expanded_file_paths
            logger.debug(f"After expansion and exclusions: {len(file_paths)} file paths")
        
        elif 'egnyte_folder_path' in data and data['egnyte_folder_path']:
            # Folder path provided - recursively list files
            folder_path = data['egnyte_folder_path']
            if not folder_path.startswith('/'):
                folder_path = '/' + folder_path
            
            # Skip if the folder itself is excluded
            if _should_exclude_path(folder_path, excluded_paths):
                return jsonify({
                    'error': 'The specified folder is excluded from review'
                }), 400
            
            include_globs = data.get('include_globs', [])
            exclude_globs = data.get('exclude_globs', [])
            
            try:
                file_list = _list_files_recursive(
                    egnyte_token=egnyte_token,
                    folder_path=folder_path,
                    include_globs=include_globs,
                    exclude_globs=exclude_globs
                )
                # Filter out excluded paths
                file_paths = [f['path'] for f in file_list if not _should_exclude_path(f['path'], excluded_paths)]
                logger.debug(f"Listed {len(file_list)} files, {len(file_paths)} after exclusions")
            except Exception as e:
                return jsonify({
                    'error': 'Failed to list files from Egnyte',
                    'error_description': str(e)
                }), 500
        
        else:
            return jsonify({
                'error': 'Must provide either file_paths or egnyte_folder_path'
            }), 400
        
        if len(file_paths) == 0:
            return jsonify({
                'error': 'No files found to process after applying exclusions'
            }), 400
        
        # Fetch all files from Egnyte
        documents = {}
        fetch_errors = {}
        
        logger.info(f"Starting to fetch {len(file_paths)} files from Egnyte...")
        for idx, file_path in enumerate(file_paths, 1):
            logger.info(f"Fetching file {idx}/{len(file_paths)}: {os.path.basename(file_path)}")
            try:
                file_content = _fetch_file_from_egnyte(egnyte_token, file_path)
                
                # Determine if file is binary (PDF) or text
                file_ext = os.path.splitext(file_path)[1].lower()
                is_pdf = file_ext == '.pdf'
                
                if is_pdf:
                    # Base64 encode PDFs
                    import base64
                    documents[file_path] = base64.b64encode(file_content).decode('utf-8')
                else:
                    # Decode text files to UTF-8
                    try:
                        documents[file_path] = file_content.decode('utf-8')
                    except UnicodeDecodeError:
                        # Fallback: try other encodings or base64 encode
                        try:
                            documents[file_path] = file_content.decode('latin-1')
                        except:
                            # Last resort: base64 encode
                            import base64
                            documents[file_path] = base64.b64encode(file_content).decode('utf-8')
                            
            except Exception as e:
                fetch_errors[file_path] = str(e)
                logger.warning(f"Could not fetch file {file_path}: {e}")
        
        if len(documents) == 0:
            return jsonify({
                'error': 'Failed to fetch any files from Egnyte',
                'fetch_errors': fetch_errors
            }), 500
        
        logger.info(f"Successfully fetched {len(documents)} files. Starting preprocessing...")
        
        # Execute workflow with fetched documents
        result = workflow.execute_constructability_review(
            documents=documents,
            selected_disciplines=selected_disciplines
        )
        
        logger.info(f"Workflow completed. Total issues found: {result.get('total_issues', 0)}")
        
        # Add fetch errors to result if any
        if fetch_errors:
            result['fetch_errors'] = fetch_errors
            result['fetch_warnings'] = f"Failed to fetch {len(fetch_errors)} file(s)"
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__
        }), 500


# New async endpoints
@app.route('/workflow/start-constructability-review', methods=['POST'])
def start_constructability_review():
    """
    Start an async constructability review job.
    
    Returns immediately with job_id. Use /workflow/status/{job_id} to check progress.
    
    Expected JSON body:
    {
        "file_paths": ["/Shared/Projects/X/drawing1.pdf", "/Shared/Projects/X/spec.csv"],
        OR
        "egnyte_folder_path": "/Shared/Projects/X",
        "include_globs": ["**/*.pdf", "**/*.csv"],
        "exclude_globs": ["**/Archive/**"],
        "excluded_paths": ["/Shared/Projects/X/Archive", "/Shared/Projects/X/old_drawing.pdf"],  // NEW: Can be files or folders
        "selected_disciplines": ["Fire Protection", "Electrical", "Structural"],
        "csv_output_folder": "/Shared/Output",
        "csv_output_filename": "results"
    }
    """
    try:
        # Cleanup old jobs
        _cleanup_old_jobs()
        
        # Extract token from Authorization header
        egnyte_token = _extract_egnyte_token()
        if not egnyte_token:
            return jsonify({
                'error': 'Missing or invalid Authorization header',
                'error_description': 'Expected Authorization: Bearer <token>'
            }), 401
        
        data = request.get_json()
        if not data:
            return jsonify({
                'error': 'Missing request body'
            }), 400
        
        if 'selected_disciplines' not in data:
            return jsonify({
                'error': 'Missing required field: selected_disciplines'
            }), 400
        
        selected_disciplines = data['selected_disciplines']
        if not isinstance(selected_disciplines, list) or len(selected_disciplines) == 0:
            return jsonify({
                'error': 'selected_disciplines must be a non-empty array'
            }), 400
        
        # Validate CSV output parameters if provided
        csv_output_folder = data.get('csv_output_folder')
        csv_output_filename = data.get('csv_output_filename')
        
        # If one is provided, both must be provided
        if (csv_output_folder and not csv_output_filename) or (csv_output_filename and not csv_output_folder):
            return jsonify({
                'error': 'Both csv_output_folder and csv_output_filename must be provided together, or neither'
            }), 400
        
        # If both are provided, verify the folder exists before starting the job
        if csv_output_folder and csv_output_filename:
            # Ensure folder path starts with /
            if not csv_output_folder.startswith('/'):
                csv_output_folder = '/' + csv_output_folder
            
            # Check if folder exists
            if not _is_egnyte_folder(egnyte_token, csv_output_folder):
                return jsonify({
                    'error': f'CSV output folder does not exist: {csv_output_folder}',
                    'error_description': 'The specified folder path does not exist in Egnyte. Please verify the path and try again.'
                }), 400
            
            logger.info(f"Verified CSV output folder exists: {csv_output_folder}")
        
        # Create job
        job_id = _create_job()
        
        # Start background processing
        logger.info(f"Starting async job {job_id} with {len(selected_disciplines)} disciplines")
        thread = threading.Thread(
            target=_process_constructability_review_async,
            args=(job_id, egnyte_token, data),
            daemon=True
        )
        thread.start()
        
        logger.info(f"Job {job_id} queued, thread started")
        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': 'Review job started. Poll /workflow/status/{job_id} for progress.'
        }), 202
        
    except Exception as e:
        return jsonify({
            'error': str(e),
            'error_type': type(e).__name__
        }), 500


@app.route('/workflow/status/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Get the status and progress of a constructability review job."""
    logger.debug(f"Poll request for job_id: {job_id}")
    job = _get_job(job_id)
    
    if not job:
        logger.warning(f"Job {job_id} not found")
        return jsonify({
            'error': 'Job not found',
            'error_description': f'Job ID {job_id} does not exist or has been deleted'
        }), 404
    
    response = {
        'job_id': job_id,
        'status': job['status'],
        'progress': job['progress'],
        'created_at': job['created_at'],
        'updated_at': job['updated_at']
    }
    
    if job['status'] == 'error':
        response['error'] = job['error']
    elif job['status'] == 'complete':
        response['message'] = 'Job completed successfully. Call /workflow/result/{job_id} to retrieve results.'
    
    logger.debug(f"Job {job_id} status: {job['status']}, stage: {job['progress'].get('current_stage', 'N/A')}")
    return jsonify(response), 200


@app.route('/workflow/result/<job_id>', methods=['GET'])
def get_job_result(job_id):
    """Get the final result of a completed constructability review job."""
    job = _get_job(job_id)
    
    if not job:
        return jsonify({
            'error': 'Job not found',
            'error_description': f'Job ID {job_id} does not exist or has been deleted'
        }), 404
    
    if job['status'] == 'processing' or job['status'] == 'queued':
        return jsonify({
            'job_id': job_id,
            'status': job['status'],
            'message': 'Job is still processing. Use /workflow/status/{job_id} to check progress.'
        }), 202
    
    if job['status'] == 'error':
        return jsonify({
            'job_id': job_id,
            'status': 'error',
            'error': job['error']
        }), 500
    
    if job['status'] == 'complete':
        return jsonify(job['result']), 200
    
    return jsonify({
        'error': 'Unknown job status',
        'status': job['status']
    }), 500


@app.route('/workflow/test-upload-csv', methods=['POST'])
def test_upload_csv():
    """
    Test endpoint to create a simple test CSV and upload it to Egnyte.
    Creates a CSV with "test" content to verify the upload functionality.
    """
    try:
        # Extract token from Authorization header
        egnyte_token = _extract_egnyte_token()
        if not egnyte_token:
            return jsonify({
                'error': 'Missing or invalid Authorization header',
                'error_description': 'Expected Authorization: Bearer <token>'
            }), 401
        
        data = request.get_json()
        if not data:
            return jsonify({
                'error': 'Missing request body'
            }), 400
        
        csv_output_folder = data.get('csv_output_folder')
        csv_output_filename = data.get('csv_output_filename')
        
        if not csv_output_folder:
            return jsonify({
                'error': 'Missing required field: csv_output_folder'
            }), 400
        
        if not csv_output_filename:
            return jsonify({
                'error': 'Missing required field: csv_output_filename'
            }), 400
        
        # Create test CSV and upload
        success, message = _create_test_csv_and_upload(
            egnyte_token,
            csv_output_folder,
            csv_output_filename
        )
        
        # Build full path for response
        if not csv_output_filename.lower().endswith('.csv'):
            csv_output_filename = csv_output_filename + '.csv'
        
        if not csv_output_folder.startswith('/'):
            csv_output_folder = '/' + csv_output_folder
        
        csv_path = f"{csv_output_folder.rstrip('/')}/{csv_output_filename}"
        
        if success:
            return jsonify({
                'success': True,
                'message': message,
                'csv_path': csv_path
            }), 200
        else:
            return jsonify({
                'success': False,
                'message': message,
                'csv_path': csv_path
            }), 500
        
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_traceback = traceback.format_exc()
        logger.error(f"Error: {error_msg}")
        logger.error(f"Traceback:\n{error_traceback}")
        return jsonify({
            'success': False,
            'error': error_msg
        }), 500


if __name__ == '__main__':
    logger.info("="*80)
    logger.info("Initializing Constructability Orchestrator...")
    logger.info(f"Port: {SharedConfig.PORT}")
    logger.info(f"Debug mode: {SharedConfig.DEBUG}")
    logger.info(f"Egnyte Base URL: {SharedConfig.EGNYTE_BASE_URL}")
    logger.info(f"Egnyte Authorization URL: {SharedConfig.EGNYTE_AUTHORIZATION_URL}")
    logger.info(f"Egnyte Token URL: {SharedConfig.EGNYTE_TOKEN_URL}")
    logger.info(f"Egnyte Client ID: {SharedConfig.EGNYTE_CLIENT_ID[:10] + '...' if SharedConfig.EGNYTE_CLIENT_ID else 'NOT_SET'}")
    logger.info("OAuth endpoints ready: /oauth/authorize, /oauth/token")
    logger.info("="*80)
    
    SharedConfig.validate()
    app.run(
        host='0.0.0.0',
        port=SharedConfig.PORT,
        debug=SharedConfig.DEBUG
    )

