import os
import shutil
import csv
import getpass
from datetime import datetime, timezone
from lxml import etree
import paramiko
import stat
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

# === SCRIPT INFO ===
SCRIPT_TITLE = "DDEX Delivery & Ingestion Tool"
SCRIPT_VERSION = "1.0.0"

# === CONFIG ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, 'profile') 
PROCESSED_DIR = os.path.join(BASE_DIR, 'processed')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
CSV_LOG = os.path.join(LOG_DIR, 'releases.csv')
TEXT_LOG = os.path.join(LOG_DIR, 'ddex_log.txt')
SCHEMA_PATH = os.path.join(BASE_DIR, 'schema', 'release-notification.xsd') # Optional

# === SFTP CONFIG ===
SFTP_HOST = os.getenv('SFTP_HOST')
SFTP_PORT = int(os.getenv('SFTP_PORT', 22))
SFTP_USERNAME = os.getenv('SFTP_USERNAME')
SFTP_PASSWORD = os.getenv('SFTP_PASSWORD')
REMOTE_UPLOAD_DIR = os.getenv('REMOTE_UPLOAD_DIR')
LOCAL_PROFILE_DIR = PROFILE_DIR

# Standard SFTP port, used automatically for delivery so the user isn't
# prompted for it.
DEFAULT_SFTP_PORT = 22

# === HELPERS ===
def is_delivery_complete(delivery_path):
    """Check whether a delivery folder has the full expected structure:
    a BatchComplete_*.xml marker, at least one UPC subfolder, and for every
    UPC subfolder both its metadata XML and a resources/ folder."""
    if not os.path.isdir(delivery_path):
        return False

    complete_files = [f for f in os.listdir(delivery_path) if f.startswith('BatchComplete_')]
    if not complete_files:
        return False

    upc_folders = [f for f in os.listdir(delivery_path) if os.path.isdir(os.path.join(delivery_path, f))]
    if not upc_folders:
        return False

    for upc in upc_folders:
        upc_dir = os.path.join(delivery_path, upc)
        xml_file = os.path.join(upc_dir, f"{upc}.xml")
        resource_dir = os.path.join(upc_dir, 'resources')
        if not os.path.exists(xml_file) or not os.path.exists(resource_dir):
            return False

    return True

def download_sftp_deliveries():
    log_message("🔄 Checking VPS for new deliveries via SFTP...")

    try:
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USERNAME, password=SFTP_PASSWORD)
        sftp = paramiko.SFTPClient.from_transport(transport)

        remote_folders = sftp.listdir(REMOTE_UPLOAD_DIR)
        if not remote_folders:
            log_message("ℹ️ No new folders found in remote upload directory.")
        else:
            for folder in remote_folders:
                remote_folder_path = f"{REMOTE_UPLOAD_DIR}/{folder}"
                local_profile_path = os.path.join(LOCAL_PROFILE_DIR, folder)
                local_processed_path = os.path.join(PROCESSED_DIR, folder)

                try:
                    if not is_remote_dir(sftp, remote_folder_path):
                        log_message(f"⚠️ Skipping '{folder}': not a directory on remote.")
                        continue

                    # 1. Check processed/ first — if it's already there and complete,
                    #    there's nothing to do.
                    if os.path.exists(local_processed_path):
                        if is_delivery_complete(local_processed_path):
                            log_message(f"⏭️ '{folder}' already in processed/ and complete. Skipping.")
                            continue
                        else:
                            log_message(
                                f"🗑️ '{folder}' exists in processed/ but is INCOMPLETE. "
                                f"Removing it to avoid duplicates before re-download."
                            )
                            shutil.rmtree(local_processed_path)
                            # Fall through — re-download into profile/ below.

                    # 2. Check profile/ — a folder here from a prior run is presumed
                    #    incomplete (interrupted download), so clear it and re-download.
                    if os.path.exists(local_profile_path):
                        log_message(
                            f"🗑️ '{folder}' already exists in profile/ (likely incomplete "
                            f"from a prior run). Removing before re-download."
                        )
                        shutil.rmtree(local_profile_path)

                    log_message(f"⬇️ Downloading delivery folder: {folder}")
                    os.makedirs(local_profile_path, exist_ok=True)
                    total_bytes = get_remote_folder_size(sftp, remote_folder_path)
                    with tqdm(
                        total=total_bytes, unit='B', unit_scale=True,
                        desc=f"Downloading {folder}"
                    ) as pbar:
                        download_folder(sftp, remote_folder_path, local_profile_path, progress_bar=pbar)
                except Exception as folder_error:
                    log_message(f"❌ Error handling folder '{folder}': {folder_error}")

        sftp.close()
        transport.close()
        log_message("✅ SFTP sync complete.")
    except Exception as e:
        log_message(f"❌ SFTP connection failed: {e}")

def is_remote_dir(sftp, path):
    try:
        return stat.S_ISDIR(sftp.stat(path).st_mode)
    except IOError:
        return False

def get_remote_listing(sftp, remote_path):
    """List a remote directory once, returning (name, is_dir, size) tuples.
    listdir_attr() returns file metadata (type, size) bundled with the
    directory listing in a single round trip, instead of a separate stat()
    call per item — much faster on directories with many files."""
    entries = []
    for attr in sftp.listdir_attr(remote_path):
        entries.append((attr.filename, stat.S_ISDIR(attr.st_mode), attr.st_size))
    return entries

def get_remote_folder_size(sftp, remote_path):
    """Total size in bytes of every file under remote_path, recursively."""
    total = 0
    for name, is_dir, size in get_remote_listing(sftp, remote_path):
        item_path = f"{remote_path}/{name}"
        if is_dir:
            total += get_remote_folder_size(sftp, item_path)
        else:
            total += size
    return total

def download_folder(sftp, remote_path, local_path, progress_bar=None):
    for name, is_dir, _size in get_remote_listing(sftp, remote_path):
        remote_item_path = f"{remote_path}/{name}"
        local_item_path = os.path.join(local_path, name)

        if is_dir:
            os.makedirs(local_item_path, exist_ok=True)
            download_folder(sftp, remote_item_path, local_item_path, progress_bar)
        else:
            if progress_bar is not None:
                # paramiko calls this with cumulative bytes for the current
                # file, so track the delta since the last call and feed that
                # delta to tqdm (which expects incremental updates).
                last_transferred = [0]

                def _report(transferred, _total, _last=last_transferred):
                    progress_bar.update(transferred - _last[0])
                    _last[0] = transferred

                sftp.get(remote_item_path, local_item_path, callback=_report)
            else:
                sftp.get(remote_item_path, local_item_path)

def remote_path_exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False

def get_folder_size(local_path):
    """Total size in bytes of every file under local_path, recursively."""
    total = 0
    for root, _dirs, files in os.walk(local_path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total

def upload_folder(sftp, local_path, remote_path, progress_bar=None):
    if not remote_path_exists(sftp, remote_path):
        try:
            sftp.mkdir(remote_path)
        except Exception as e:
            # Some SFTP servers (notably object-storage-backed gateways like
            # AWS Transfer Family/S3) don't support creating an empty
            # directory explicitly — "folders" only come into existence as a
            # side effect of uploading a file into that path. Rather than
            # hard-failing here, log it and let the file uploads below
            # attempt to create the path implicitly.
            log_message(
                f"⚠️ Could not explicitly create remote directory '{remote_path}' "
                f"({e}). This is expected on some servers — continuing, since "
                f"uploading files into it may create the path implicitly."
            )

    for item in os.listdir(local_path):
        local_item_path = os.path.join(local_path, item)
        remote_item_path = f"{remote_path}/{item}"

        if os.path.isdir(local_item_path):
            upload_folder(sftp, local_item_path, remote_item_path, progress_bar)
        else:
            try:
                if progress_bar is not None:
                    # paramiko calls this with cumulative bytes for the current
                    # file, so we track the delta since the last call and feed
                    # that delta to tqdm (which expects incremental updates).
                    last_transferred = [0]

                    def _report(transferred, _total, _last=last_transferred):
                        progress_bar.update(transferred - _last[0])
                        _last[0] = transferred

                    sftp.put(local_item_path, remote_item_path, callback=_report)
                else:
                    sftp.put(local_item_path, remote_item_path)
            except Exception as e:
                raise RuntimeError(
                    f"Failed to upload '{local_item_path}' to '{remote_item_path}': {e}. "
                    f"Check that the destination folder exists and that this "
                    f"account has write permission there."
                ) from e

def deliver_via_sftp(host, port, username, password, local_delivery_path, remote_upload_dir):
    log_message(f"🔄 Connecting to {host}:{port} to deliver '{local_delivery_path}'...")

    if not os.path.isdir(local_delivery_path):
        log_message(f"❌ Local delivery path does not exist or is not a directory: {local_delivery_path}")
        return

    try:
        transport = paramiko.Transport((host, port))
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)

        folder_name = os.path.basename(os.path.normpath(local_delivery_path))
        total_bytes = get_folder_size(local_delivery_path)
        log_message(
            f"⬆️ Uploading contents of '{folder_name}' ({total_bytes / (1024*1024):.1f} MB) "
            f"directly into {remote_upload_dir}..."
        )

        with tqdm(total=total_bytes, unit='B', unit_scale=True, desc=f"Uploading {folder_name}") as pbar:
            upload_folder(sftp, local_delivery_path, remote_upload_dir, progress_bar=pbar)

        sftp.close()
        transport.close()
        log_message(f"✅ Delivery from '{folder_name}' uploaded successfully.")
    except Exception as e:
        log_message(f"❌ SFTP delivery failed: {e}")

def log_message(message):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")
    with open(TEXT_LOG, 'a', encoding='utf-8') as f:
        f.write(f"[{timestamp}] {message}\n")

def validate_xml(xml_path):
    try:
        if not os.path.exists(SCHEMA_PATH):
            log_message("❗ Schema file not found. Skipping validation.")
            return True
        with open(SCHEMA_PATH, 'rb') as xsd_file:
            schema_doc = etree.parse(xsd_file)
            schema = etree.XMLSchema(schema_doc)
            xml_doc = etree.parse(xml_path)
            schema.assertValid(xml_doc)
            return True
    except Exception as e:
        log_message(f" XML validation failed: {e}")
        return False

def parse_and_log_metadata(xml_path):
    try:
        tree = etree.parse(xml_path)
        root = tree.getroot()
        ns = {'ern': 'http://ddex.net/xml/ern/411'}

        message_id = root.findtext('.//ern:MessageId', namespaces=ns)
        release_title = root.findtext('.//ern:Release/ern:DisplayTitleText', namespaces=ns)
        isrc = root.findtext('.//ern:SoundRecording/ern:ResourceId/ern:ISRC', namespaces=ns)

        with open(CSV_LOG, 'a', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            if os.stat(CSV_LOG).st_size == 0:
                writer.writerow(['Timestamp', 'MessageId', 'ReleaseTitle', 'ISRC'])
            writer.writerow([datetime.now(timezone.utc).isoformat(), message_id, release_title, isrc])

        log_message(f" Parsed metadata: MessageId={message_id}, Title={release_title}, ISRC={isrc}")
    except Exception as e:
        log_message(f"Failed to parse metadata: {e}")


# === MAIN INGESTION FUNCTION ===
def ingest_ddex_deliveries():
    log_message("🔍 Starting DDEX ingestion check...")

    if not os.path.exists(PROFILE_DIR):
        log_message("❗ Profile directory does not exist.")
        return

    deliveries = [d for d in os.listdir(PROFILE_DIR) if os.path.isdir(os.path.join(PROFILE_DIR, d))]
    log_message(f"📂 Found folders: {deliveries}")

    if not deliveries:
        log_message("No incoming deliveries found in profile/.")
        return

    for folder in deliveries:
        delivery_path = os.path.join(PROFILE_DIR, folder)
        
        log_message("=" * 60)
        log_message(f"🚚 Processing delivery: {folder}")
        log_message("=" * 60)
    
        complete_files = [f for f in os.listdir(delivery_path) if f.startswith('BatchComplete_')]
        if complete_files:
            log_message(f"✅ Found BatchComplete file: {complete_files[0]}")
        else:
            log_message(f"❌ No BatchComplete_*.xml file found in '{folder}'. Skipping.")
            continue

        upc_folders = [f for f in os.listdir(delivery_path) if os.path.isdir(os.path.join(delivery_path, f))]
        if not upc_folders:
            log_message(f"❌ No UPC folder found in '{folder}'. Skipping.")
            continue

        log_message(f"✅ Found {len(upc_folders)} UPC folder(s): {upc_folders}")

        # Process every UPC in this batch, not just the first.
        failed_upcs = []
        succeeded_upcs = []

        for upc in upc_folders:
            upc_dir = os.path.join(delivery_path, upc)
            xml_file = os.path.join(upc_dir, f"{upc}.xml")
            resource_dir = os.path.join(upc_dir, 'resources')

            if os.path.exists(xml_file):
                log_message(f"✅ [{upc}] Found metadata XML: {xml_file}")
            else:
                log_message(f"❌ [{upc}] Missing metadata XML file: {xml_file}")

            if os.path.exists(resource_dir):
                log_message(f"✅ [{upc}] Found resources folder: {resource_dir}")
            else:
                log_message(f"❌ [{upc}] Missing resources folder in: {upc_dir}")

            if not os.path.exists(xml_file) or not os.path.exists(resource_dir):
                log_message(f"⚠️ [{upc}] Missing XML or resources folder. Skipping this UPC.")
                failed_upcs.append(upc)
                continue

            # Validate XML
            if not validate_xml(xml_file):
                log_message(f"⚠️ [{upc}] Failed XML schema validation. Skipping this UPC.")
                failed_upcs.append(upc)
                continue

            # Parse metadata
            parse_and_log_metadata(xml_file)
            succeeded_upcs.append(upc)

        # Only move the whole delivery to processed/ if every UPC in it succeeded.
        # A partial failure leaves the delivery in profile/ so it can be inspected
        # and retried, rather than silently losing the failed UPC(s).
        if failed_upcs:
            log_message(
                f"❌ Delivery '{folder}' left in place: {len(failed_upcs)} of "
                f"{len(upc_folders)} UPC(s) failed ({failed_upcs}); "
                f"{len(succeeded_upcs)} succeeded ({succeeded_upcs})."
            )
            continue

        processed_path = os.path.join(PROCESSED_DIR, folder)
        shutil.move(delivery_path, processed_path)
        log_message(
            f"📦 Delivery '{folder}' successfully processed and moved "
            f"({len(succeeded_upcs)} UPC(s): {succeeded_upcs})."
        )

# === CLI PROMPTS ===
def print_banner():
    banner_line = "=" * 50
    print(f"\n{banner_line}")
    print(f"  {SCRIPT_TITLE}")
    print(f"  Version {SCRIPT_VERSION}")
    print(f"{banner_line}")

def prompt_mode():
    while True:
        choice = input(
            "\nWhat would you like to do?\n"
            "  [1] Deliver (upload a delivery to a remote SFTP server)\n"
            "  [2] Ingest (download and process incoming deliveries)\n"
            "Enter 1 or 2: "
        ).strip()
        if choice == '1':
            return 'deliver'
        elif choice == '2':
            return 'ingest'
        print("Please enter 1 or 2.")

def prompt_delivery_config():
    print("\n--- Delivery setup ---")
    host = input("SFTP server host/IP to deliver to: ").strip()
    username = input("SFTP username: ").strip()
    password = getpass.getpass("SFTP password: ")
    remote_upload_dir = input("Remote upload directory (e.g. /uploads): ").strip()
    local_delivery_path = input(
        f"Local folder to deliver (default: {PROFILE_DIR}): "
    ).strip() or PROFILE_DIR

    # Port is picked automatically rather than prompted for.
    port = DEFAULT_SFTP_PORT
    log_message(f"ℹ️ Using default SFTP port {port}.")

    return {
        'host': host,
        'port': port,
        'username': username,
        'password': password,
        'remote_upload_dir': remote_upload_dir,
        'local_delivery_path': local_delivery_path,
    }

# === RUN SCRIPT ===
if __name__ == "__main__":
    print_banner()
    mode = prompt_mode()

    if mode == 'deliver':
        config = prompt_delivery_config()
        deliver_via_sftp(
            host=config['host'],
            port=config['port'],
            username=config['username'],
            password=config['password'],
            local_delivery_path=config['local_delivery_path'],
            remote_upload_dir=config['remote_upload_dir'],
        )
    else:
        download_sftp_deliveries()
        ingest_ddex_deliveries()