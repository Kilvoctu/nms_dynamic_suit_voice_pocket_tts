# tray_ui.py
import threading
from pathlib import Path
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem
from modular.config import SuitVoiceConfig

class TrayUI:
    def __init__(self, config: SuitVoiceConfig, watch_target):
        self.config = config
        self.watch_target = watch_target
        self.running = True
        self.current_status = "Initializing..."

        # Load original transparent base icon
        self.base_img = self._load_base_image()

        # Generate the crimson background "caution" variant for initialization state
        init_img = self._generate_tinted_image(self.base_img, color=(220, 20, 60, 255), is_ready=False)  # Semi-transparent Crimson

        self.icon = Icon(
            "NMS_DynamicSuitVoice",
            init_img,
            title=f"Dynamic Suit Voice - {self.current_status}",
            menu=self._make_menu()
        )

    def _load_base_image(self) -> Image.Image:
        try:
            icon_path_str = self.config.icon_image
            if icon_path_str and Path(icon_path_str).exists():
                return Image.open(icon_path_str).convert("RGBA")
        except Exception as e:
            print(f"Warning: Could not load icon asset: {e}.")
        return Image.new('RGBA', (64, 64), color=(0, 0, 0, 0))

    @staticmethod
    def _generate_tinted_image(base_img: Image.Image, color: tuple, is_ready: bool) -> Image.Image:
        """Applies a composite yellow background pad only during boot, preserving transparency when ready."""
        if is_ready:
            # Drop all tint processes and return the natural transparent asset directly
            return base_img

        w, h = base_img.size
        bg = Image.new("RGBA", (w, h), color=(0, 0, 0, 0))
        draw = ImageDraw.Draw(bg)

        # Yellow caution card layer
        draw.rounded_rectangle([2, 2, w-3, h-3], radius=max(int(w*0.15), 4), fill=color)

        # Flatten transparent icon layer over the top of the card padding context
        final_composite = Image.alpha_composite(bg, base_img)
        return final_composite

    def update_status(self, status_msg: str, is_ready: bool = False):
        """Thread-safe status update hook. Modifies the mouseover tooltip text and icon graphics."""
        self.current_status = status_msg
        self.icon.title = f"Dynamic Suit Voice - {self.current_status}"

        if is_ready:
            # Revert back to the natural clean base image format (transparent backgrounds)
            self.icon.icon = self.base_img.convert("RGB")
        else:
            # Re-apply crimson tint with updated status values if needed
            init_img = self._generate_tinted_image(self.base_img, color=(220, 20, 60, 255), is_ready=False)
            self.icon.icon = init_img

    # === Menu builders ===
    def _make_menu(self):
        return Menu(
            MenuItem('Quit', self.on_quit)
        )

    # === Actions ===
    def on_quit(self, _icon, _item):
        self.running = False
        self.icon.stop()


    def run(self):
        # Fire watchdog engine initialization thread separate from pystray runtime loop
        watcher = threading.Thread(target=self.watch_target, args=(self,), daemon=True)
        watcher.start()
        self.icon.run()
