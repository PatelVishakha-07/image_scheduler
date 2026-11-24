"""
Tkinter Login + GitHub Image Uploader (static credentials, no database).

Flow:
  1. Login screen (centered window, eye button to show/hide password)
  2. After correct login -> Upload page: paste a file path or browse for an
     image / PDF, preview it, then click "Upload to GitHub".
  3. The app logs in to GitHub with your Personal Access Token, creates the
     repository if it doesn't exist yet, and uploads the file into it.

GitHub setup (one time):
  - GitHub > Settings > Developer settings > Personal access tokens >
    Tokens (classic) > Generate new token, tick the "repo" scope.
  - Set it as an environment variable named GITHUB_TOKEN
        Windows (PowerShell):  setx GITHUB_TOKEN "ghp_xxxxxxxx"   (then reopen terminal)
        Mac/Linux:             export GITHUB_TOKEN="ghp_xxxxxxxx"
    OR create a file named .env next to this script containing:
        GITHUB_TOKEN=ghp_xxxxxxxx
    (add .env to .gitignore so it is never committed or shared).
  - Choose the repository name in GITHUB_REPO below.

Run:  python login_app.py      (default login: admin / Admin@123)

Optional (for image previews of JPG/WEBP/BMP etc.):  pip install pillow
"""

import base64
import hmac
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageTk  # optional
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ---------------------------------------------------------------- .env support
def load_env_file(filename: str = ".env"):
    """Read KEY=VALUE lines from a .env file next to this script (if present)
    and add them to the environment. Real environment variables win."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

# ---------------------------------------------------------------- settings
USERS = {
    "admin": "Admin@123",
    "user": "User@123",
}

# ---- GitHub settings ----
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")   # or paste: "ghp_xxxxxxxx"
GITHUB_REPO = "image-uploads"    # repository name (created automatically if missing)
GITHUB_PRIVATE = True            # True = private repo, False = public (used only when creating)
GITHUB_FOLDER = "uploads"        # folder inside the repo ("" = repo root)
MAX_UPLOAD_MB = 25               # GitHub's contents API works best below this size
GITHUB_API = "https://api.github.com"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
PDF_EXTS = {".pdf"}
ALLOWED_EXTS = IMAGE_EXTS | PDF_EXTS

# colours
BG = "#eef1f6"
CARD = "#ffffff"
PRIMARY = "#0f766e"
PRIMARY_DARK = "#115e59"
PRIMARY_LIGHT = "#e6f4f2"
TEXT = "#1f2937"
MUTED = "#6b7280"
ERROR = "#dc2626"
SUCCESS = "#16a34a"

# upload page button colours
INDIGO = "#4f46e5"
INDIGO_DARK = "#4338ca"
GREEN = "#16a34a"
GREEN_DARK = "#15803d"
RED = "#dc2626"
RED_LIGHT = "#fee2e2"


# ---------------------------------------------------------------- helpers
def authenticate(username: str, password: str) -> bool:
    """True only if the username exists and the password matches."""
    stored = USERS.get(username)
    if stored is None:
        return False
    return hmac.compare_digest(stored.encode("utf-8"), password.encode("utf-8"))


def center_window(win: tk.Misc, min_width: int, min_height: int):
    """Size the window to fit its content (never smaller than the given
    minimum) and place it in the middle of the screen."""
    win.update_idletasks()
    width = max(min_width, win.winfo_reqwidth())
    height = max(min_height, win.winfo_reqheight())
    x = (win.winfo_screenwidth() - width) // 2
    y = (win.winfo_screenheight() - height) // 2
    win.geometry(f"{width}x{height}+{x}+{y}")


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class GitHubError(Exception):
    """Readable error message to show to the user."""


def github_request(method: str, url: str, token: str, payload=None):
    """Call the GitHub REST API. Returns (status_code, json_body)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "tkinter-uploader")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GitHubError("Could not reach GitHub. Check your internet connection.")


def github_upload(file_path: str, progress) -> dict:
    """Log in, make sure the repo exists, upload the file. Returns info dict."""
    token = GITHUB_TOKEN.strip()
    if not token:
        raise GitHubError(
            "GitHub token not set. Create a Personal Access Token and set it as "
            "the GITHUB_TOKEN environment variable (see top of the script)."
        )

    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise GitHubError(f"File is {size_mb:.1f} MB. The limit is {MAX_UPLOAD_MB} MB.")

    # 1) log in / identify the account
    progress("Logging in to GitHub…")
    status, user = github_request("GET", f"{GITHUB_API}/user", token)
    if status == 401:
        raise GitHubError("GitHub login failed: the token is invalid or has expired.")
    if status != 200:
        raise GitHubError(f"GitHub login failed ({status}): {user.get('message', '')}")
    owner = user["login"]

    # 2) make sure the repository exists
    progress(f"Logged in as {owner}. Checking repository…")
    status, repo = github_request("GET", f"{GITHUB_API}/repos/{owner}/{GITHUB_REPO}", token)
    if status == 404:
        progress("Repository not found. Creating it…")
        status, repo = github_request(
            "POST", f"{GITHUB_API}/user/repos", token,
            {
                "name": GITHUB_REPO,
                "private": GITHUB_PRIVATE,
                "auto_init": True,  # creates a first commit so uploads work
                "description": "Files uploaded from my Tkinter app",
            },
        )
        if status != 201:
            raise GitHubError(
                f"Could not create the repository ({status}): {repo.get('message', '')}"
            )
        time.sleep(2)  # give GitHub a moment to finish setting it up
    elif status != 200:
        raise GitHubError(f"Could not open the repository ({status}): {repo.get('message', '')}")
    branch = repo.get("default_branch", "main")

    # 3) upload the file (unique name so nothing is overwritten)
    progress("Uploading file…")
    with open(file_path, "rb") as f:
        content = base64.b64encode(f.read()).decode("ascii")

    stem, ext = os.path.splitext(os.path.basename(file_path))
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "file"
    name = f"{safe_stem}_{datetime.now():%Y%m%d_%H%M%S}{ext.lower()}"
    folder = GITHUB_FOLDER.strip("/")
    repo_path = f"{folder}/{name}" if folder else name

    status, result = github_request(
        "PUT",
        f"{GITHUB_API}/repos/{owner}/{GITHUB_REPO}/contents/{urllib.parse.quote(repo_path)}",
        token,
        {"message": f"Upload {name}", "content": content, "branch": branch},
    )
    if status not in (200, 201):
        hint = " (check that your token has the 'repo' scope)" if status in (403, 404) else ""
        raise GitHubError(f"Upload failed ({status}): {result.get('message', '')}{hint}")

    return {
        "owner": owner,
        "repo": GITHUB_REPO,
        "path": repo_path,
        "url": result.get("content", {}).get("html_url", f"https://github.com/{owner}/{GITHUB_REPO}"),
    }


def setup_styles(root: tk.Tk):
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=CARD, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
    style.configure("Sub.TLabel", foreground=MUTED, font=("Segoe UI", 10))
    style.configure("Error.TLabel", foreground=ERROR)
    style.configure("Success.TLabel", foreground=SUCCESS)

    style.configure("TEntry", padding=8, fieldbackground="#f9fafb", bordercolor="#d1d5db")
    style.map("TEntry", bordercolor=[("focus", PRIMARY)])

    # main button: solid teal, white bold text
    style.configure(
        "Primary.TButton",
        background=PRIMARY, foreground="white", font=("Segoe UI", 10, "bold"),
        padding=(12, 10), borderwidth=0, focusthickness=0,
        lightcolor=PRIMARY, darkcolor=PRIMARY, bordercolor=PRIMARY,
    )
    style.map(
        "Primary.TButton",
        background=[("active", PRIMARY_DARK), ("disabled", "#99d5cf")],
        lightcolor=[("active", PRIMARY_DARK)],
        darkcolor=[("active", PRIMARY_DARK)],
        bordercolor=[("active", PRIMARY_DARK)],
    )

    # upload page: Browse = solid indigo
    style.configure(
        "Browse.TButton",
        background=INDIGO, foreground="white", font=("Segoe UI", 10, "bold"),
        padding=(14, 9), borderwidth=0, focusthickness=0,
        lightcolor=INDIGO, darkcolor=INDIGO, bordercolor=INDIGO,
    )
    style.map(
        "Browse.TButton",
        background=[("active", INDIGO_DARK)],
        lightcolor=[("active", INDIGO_DARK)],
        darkcolor=[("active", INDIGO_DARK)],
        bordercolor=[("active", INDIGO_DARK)],
    )

    # upload page: Upload = solid green
    style.configure(
        "Upload.TButton",
        background=GREEN, foreground="white", font=("Segoe UI", 11, "bold"),
        padding=(12, 11), borderwidth=0, focusthickness=0,
        lightcolor=GREEN, darkcolor=GREEN, bordercolor=GREEN,
    )
    style.map(
        "Upload.TButton",
        background=[("active", GREEN_DARK)],
        lightcolor=[("active", GREEN_DARK)],
        darkcolor=[("active", GREEN_DARK)],
        bordercolor=[("active", GREEN_DARK)],
    )

    # upload page: Logout = white with red outline and red text
    style.configure(
        "Logout.TButton",
        background=CARD, foreground=RED, font=("Segoe UI", 10, "bold"),
        padding=(12, 8), borderwidth=1, focusthickness=0,
        lightcolor=CARD, darkcolor=CARD, bordercolor=RED,
    )
    style.map(
        "Logout.TButton",
        background=[("active", RED_LIGHT)],
        lightcolor=[("active", RED_LIGHT)],
        darkcolor=[("active", RED_LIGHT)],
    )

    # eye button beside the password box
    style.configure(
        "Eye.TButton",
        background=CARD, foreground=PRIMARY, font=("Segoe UI Emoji", 11),
        padding=(8, 6), borderwidth=1, focusthickness=0,
        lightcolor=CARD, darkcolor=CARD, bordercolor="#d1d5db",
    )
    style.map(
        "Eye.TButton",
        background=[("active", PRIMARY_LIGHT)],
        lightcolor=[("active", PRIMARY_LIGHT)],
        darkcolor=[("active", PRIMARY_LIGHT)],
        bordercolor=[("active", PRIMARY)],
    )

    style.configure("TCheckbutton", background=CARD)


# ---------------------------------------------------------------- app
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.preview_image = None  # keep reference so Tk doesn't garbage collect it
        root.configure(bg=BG)
        setup_styles(root)
        self.show_login()

    def clear_window(self):
        self.root.unbind("<Return>")
        for widget in self.root.winfo_children():
            widget.destroy()

    # ------------------------------------------------------------ login page
    def show_login(self):
        self.clear_window()
        self.root.title("Login")
        self.root.resizable(False, False)

        outer = ttk.Frame(self.root)
        outer.pack(expand=True, fill="both", padx=25, pady=25)

        card = ttk.Frame(outer, style="Card.TFrame", padding=28)
        card.pack(expand=True, fill="both")

        ttk.Label(card, text="🔐", font=("Segoe UI Emoji", 30)).pack()
        ttk.Label(card, text="Welcome Back", style="Title.TLabel").pack(pady=(4, 0))
        ttk.Label(card, text="Sign in to continue", style="Sub.TLabel").pack(pady=(0, 20))

        # username
        ttk.Label(card, text="Username").pack(anchor="w")
        self.username_entry = ttk.Entry(card, font=("Segoe UI", 11))
        self.username_entry.pack(fill="x", pady=(4, 14))

        # password + eye button beside it
        ttk.Label(card, text="Password").pack(anchor="w")
        pw_row = ttk.Frame(card, style="Card.TFrame")
        pw_row.pack(fill="x", pady=(4, 6))

        self.password_entry = ttk.Entry(pw_row, show="•", font=("Segoe UI", 11))
        self.password_entry.pack(side="left", fill="x", expand=True)

        self.password_visible = False
        self.eye_btn = ttk.Button(
            pw_row, text="👁", width=3, style="Eye.TButton",
            command=self.toggle_password, cursor="hand2",
        )
        self.eye_btn.pack(side="left", padx=(6, 0), fill="y")

        # status message
        self.status = ttk.Label(card, text="", style="Error.TLabel", wraplength=320)
        self.status.pack(pady=(6, 10))

        ttk.Button(
            card, text="Login", style="Primary.TButton",
            command=self.login, cursor="hand2",
        ).pack(fill="x")

        self.root.bind("<Return>", lambda event: self.login())
        self.username_entry.focus()
        center_window(self.root, 400, 520)

    def toggle_password(self):
        self.password_visible = not self.password_visible
        if self.password_visible:
            self.password_entry.config(show="")
            self.eye_btn.config(text="🙈")
        else:
            self.password_entry.config(show="•")
            self.eye_btn.config(text="👁")

    def login(self):
        username = self.username_entry.get().strip()
        password = self.password_entry.get()

        if not username or not password:
            self.status.config(text="Please enter username and password.")
            return

        if authenticate(username, password):
            self.show_upload_page(username)
        else:
            self.password_entry.delete(0, tk.END)
            self.status.config(text="Invalid username or password.")

    # ----------------------------------------------------------- upload page
    def show_upload_page(self, username: str):
        self.clear_window()
        self.root.title("Upload File")
        self.root.resizable(False, False)

        outer = ttk.Frame(self.root)
        outer.pack(expand=True, fill="both", padx=25, pady=25)

        card = ttk.Frame(outer, style="Card.TFrame", padding=26)
        card.pack(expand=True, fill="both")

        # header
        header = ttk.Frame(card, style="Card.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text=f"Hello, {username} 👋", style="Title.TLabel",
                  font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Button(header, text="Logout", style="Logout.TButton",
                   command=self.show_login, cursor="hand2").pack(side="right")

        ttk.Label(card, text="Upload an image or PDF to GitHub", style="Sub.TLabel").pack(
            anchor="w", pady=(2, 18))

        # path + browse
        ttk.Label(card, text="File path").pack(anchor="w")
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 4))

        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(row, textvariable=self.path_var, font=("Segoe UI", 10))
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.path_entry.bind("<Return>", lambda e: self.load_file(self.path_var.get()))
        self.path_entry.bind("<FocusOut>", lambda e: self.load_file(self.path_var.get(), quiet=True))

        ttk.Button(row, text="Browse…", style="Browse.TButton",
                   command=self.browse_file, cursor="hand2").pack(side="left", padx=(8, 0))

        ttk.Label(
            card, style="Sub.TLabel",
            text="Paste a path or click Browse. Supported: JPG, PNG, GIF, BMP, WEBP, PDF",
        ).pack(anchor="w", pady=(0, 14))

        # preview box
        preview_box = tk.Frame(card, bg="#f3f4f6", height=230, highlightthickness=1,
                               highlightbackground="#d1d5db")
        preview_box.pack(fill="x")
        preview_box.pack_propagate(False)
        self.preview_label = tk.Label(
            preview_box, text="No file selected", bg="#f3f4f6", fg=MUTED,
            font=("Segoe UI", 11), compound="top",
        )
        self.preview_label.pack(expand=True)

        # file info + status
        self.info_label = ttk.Label(card, text="", style="Sub.TLabel", wraplength=480)
        self.info_label.pack(anchor="w", pady=(10, 0))
        self.upload_status = ttk.Label(card, text="", style="Success.TLabel", wraplength=480)
        self.upload_status.pack(anchor="w", pady=(4, 10))

        self.upload_btn = ttk.Button(card, text="Upload to GitHub", style="Upload.TButton",
                                     command=self.upload_file, cursor="hand2")
        self.upload_btn.pack(fill="x", side="bottom")

        self.selected_file = None
        center_window(self.root, 560, 680)

    def browse_file(self):
        path = filedialog.askopenfilename(
            title="Select an image or PDF",
            filetypes=[
                ("Images and PDF", "*.jpg *.jpeg *.png *.gif *.bmp *.webp *.pdf"),
                ("Images", "*.jpg *.jpeg *.png *.gif *.bmp *.webp"),
                ("PDF files", "*.pdf"),
            ],
        )
        if path:
            self.path_var.set(path)
            self.load_file(path)

    def load_file(self, raw_path: str, quiet: bool = False) -> bool:
        """Validate the path, update info + preview. Returns True if valid."""
        path = raw_path.strip().strip('"').strip("'")  # handles "Copy as path"
        self.selected_file = None
        self.upload_status.config(text="")

        if not path:
            if not quiet:
                self.show_error("Please enter or select a file path.")
            return False
        if not os.path.isfile(path):
            self.reset_preview("No file selected")
            if not quiet:
                self.show_error("File not found. Check the path and try again.")
            return False

        ext = os.path.splitext(path)[1].lower()
        if ext not in ALLOWED_EXTS:
            self.reset_preview("Unsupported file type")
            if not quiet:
                self.show_error("Only image files and PDFs are allowed.")
            return False

        self.path_var.set(path)
        self.selected_file = path
        size = human_size(os.path.getsize(path))
        self.info_label.config(text=f"{os.path.basename(path)}  •  {size}  •  {ext[1:].upper()}")
        self.show_preview(path, ext)
        return True

    def show_error(self, message: str):
        self.upload_status.config(text=message, style="Error.TLabel")

    def show_ok(self, message: str):
        self.upload_status.config(text=message, style="Success.TLabel")

    def reset_preview(self, text: str):
        self.preview_image = None
        self.preview_label.config(image="", text=text)
        self.info_label.config(text="")

    def show_preview(self, path: str, ext: str):
        if ext in PDF_EXTS:
            self.preview_image = None
            self.preview_label.config(image="", text="📄\nPDF document selected",
                                      font=("Segoe UI", 13))
            return

        image = None
        try:
            if PIL_AVAILABLE:
                img = Image.open(path)
                img.thumbnail((440, 210))
                image = ImageTk.PhotoImage(img)
            elif ext in {".png", ".gif"}:  # Tk can open these natively
                img = tk.PhotoImage(file=path)
                factor = max(1, img.width() // 440 + 1, img.height() // 210 + 1)
                image = img.subsample(factor, factor)
        except Exception:
            image = None

        if image:
            self.preview_image = image
            self.preview_label.config(image=image, text="")
        else:
            self.preview_image = None
            hint = "" if PIL_AVAILABLE else "\n(install Pillow for preview: pip install pillow)"
            self.preview_label.config(image="", text=f"🖼️\nImage selected{hint}",
                                      font=("Segoe UI", 11))

    def upload_file(self):
        # make sure whatever is typed in the box is validated first
        if not self.load_file(self.path_var.get()):
            return
        self.upload_btn.config(state="disabled", text="Uploading…")
        self.upload_status.config(text="Starting…", style="Sub.TLabel")

        self.upload_queue = queue.Queue()
        threading.Thread(
            target=self.upload_worker,
            args=(self.selected_file, self.upload_queue),
            daemon=True,
        ).start()
        self.root.after(100, self.poll_upload)

    def upload_worker(self, path: str, q: queue.Queue):
        """Runs in a background thread so the window never freezes."""
        try:
            result = github_upload(path, lambda msg: q.put(("progress", msg)))
            q.put(("done", result))
        except GitHubError as exc:
            q.put(("error", str(exc)))
        except Exception as exc:
            q.put(("error", f"Unexpected error: {exc}"))

    def poll_upload(self):
        if not self.upload_btn.winfo_exists():  # user logged out meanwhile
            return
        finished = False
        try:
            while True:
                kind, data = self.upload_queue.get_nowait()
                if kind == "progress":
                    self.upload_status.config(text=data, style="Sub.TLabel")
                elif kind == "done":
                    finished = True
                    self.on_upload_done(data)
                elif kind == "error":
                    finished = True
                    self.on_upload_error(data)
        except queue.Empty:
            pass
        if not finished:
            self.root.after(100, self.poll_upload)

    def on_upload_done(self, info: dict):
        self.upload_btn.config(state="normal", text="Upload to GitHub")
        self.show_ok(f"✔ Uploaded to {info['owner']}/{info['repo']}/{info['path']}")
        if messagebox.askyesno(
            "Upload complete",
            f"File uploaded to GitHub:\n{info['url']}\n\nOpen it in your browser?",
        ):
            webbrowser.open(info["url"])

    def on_upload_error(self, message: str):
        self.upload_btn.config(state="normal", text="Upload to GitHub")
        self.show_error(message)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()