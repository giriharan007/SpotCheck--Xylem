"""
gui/theme.py

Single source of truth for SpotCheck's visual identity.

Previously the Xylem palette was declared twice — once in app_gui.py and again
in region_inspector.py — and the font resolver existed only in app_gui.py, so
the Region Inspector silently rendered in CustomTkinter's default typeface.
Both windows now import from here.

Styled according to official Xylem Brand Identity Guidelines:
  - Primary Palette  : Xylem Blue (#007DA3), Dependable Blue (#003E51),
                       Clarity Blue (#67DFFF), Dynamic Green (#61D604)
  - Secondary Palette: Inspired Teal (#20846F), Uplifting Aqua (#29CCBB),
                       Resilient Purple (#6600C5), Vivid Magenta (#D300F2),
                       Radiant Orange (#F96C00)
  - Neutral Palette  : Dark Gray (#555555), Medium Gray (#A8A8A8),
                       Light Gray (#DBDBDB), White (#FFFFFF), Black (#000000)
  - Typography       : Roboto preferred, Arial as Xylem-approved desktop fallback
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# ==============================================================================
# OFFICIAL XYLEM BRAND PALETTE
# ==============================================================================

# Primary Palette
XYLEM_BLUE       = "#007DA3"   # PMS 7704C - Dominant Primary Color
DEPENDABLE_BLUE  = "#003E51"   # PMS 3035C - Second Dominant (Headings, Heavy Cards)
CLARITY_BLUE     = "#67DFFF"   # PMS 2197C - Vibrant Energy Accent
DYNAMIC_GREEN    = "#61D604"   # PMS 2287C - Energy, Success & Action Accent

# Secondary Palette (Specialized Functional & Visual Accents)
INSPIRED_TEAL    = "#20846F"   # PMS 569C
UPLIFTING_AQUA   = "#29CCBB"   # PMS 3255C
RESILIENT_PURPLE = "#6600C5"   # PMS 2091C
VIVID_MAGENTA    = "#D300F2"   # PMS Purple C
RADIANT_ORANGE   = "#F96C00"   # PMS 1505C - Warnings / Processing

# Neutral Palette
NEUTRAL_BLACK    = "#000000"
NEUTRAL_DARK_GR  = "#555555"   # PMS 425 - Secondary Muted Text
NEUTRAL_MED_GR   = "#A8A8A8"   # PMS Cool Gray 6 - Dividers
NEUTRAL_LIGHT_GR = "#DBDBDB"   # PMS Cool Gray 1 - Borders / Containers
NEUTRAL_WHITE    = "#FFFFFF"

# Functional UI Surface Colors
UI_BG_CANVAS     = "#EEF5FA"   # Soft cool neutral background
UI_CARD_BG       = "#FFFFFF"   # Card surface
UI_CARD_WELL     = "#E2EDF7"   # Secondary container well
UI_BORDER        = "#D0DFEB"   # Soft blue-gray divider border
UI_HOVER_BLUE    = "#0095C2"   # Interactive button hover blue
UI_DARK_HOVER    = "#002834"   # Dependable blue hover

# Rotating outline colors for user-defined inspection regions
REGION_COLORS = [
    XYLEM_BLUE,
    INSPIRED_TEAL,
    RESILIENT_PURPLE,
    RADIANT_ORANGE,
    VIVID_MAGENTA,
    DEPENDABLE_BLUE,
    DYNAMIC_GREEN,
]


# Small text on a tinted surface. Xylem Blue on the pale wells measures 4.0:1
# and on the canvas 4.3:1 - under the 4.5:1 floor at the 9-11px sizes this app
# uses it for. Dependable Blue clears 10:1 on both, so captions and status text
# use this rather than the accent.
TEXT_ON_LIGHT = DEPENDABLE_BLUE

# White is unreadable on Radiant Orange (2.9:1) - it is a light colour. Near
# black gives 7.0:1 and is what the destructive buttons use.
TEXT_ON_ORANGE = NEUTRAL_BLACK

# CTk's stock disabled grey is #bdbdbd, which on a white button is 1.9:1 -
# effectively invisible, and the reason "Open Output Folder" could not be read
# before a run had produced anything.
TEXT_DISABLED = NEUTRAL_DARK_GR


# ==============================================================================
# SEGMENTED CONTROLS
# ==============================================================================
# CustomTkinter's CTkSegmentedButton - and the tab strip of CTkTabview, which is
# one - has a SINGLE text_color shared by every segment, selected or not. So the
# selected and unselected fills cannot be one light and one dark: whichever
# text colour is chosen, it disappears against one of them. That is exactly what
# had happened here, with white text sitting on a near-white unselected fill.
#
# The fix is to keep both fills dark enough for white text and let brightness
# carry the selection instead:
#
#     unselected  #003E51  white text, contrast 11.6:1
#     selected    #007DA3  white text, contrast  4.7:1   (AA for normal text)
#
# Anything changed here must keep both states above 4.5:1 against the text.

def segmented_button_colors():
    """Colour arguments for a CTkSegmentedButton, legible in every state."""
    return {
        "fg_color": UI_BORDER,                      # the gap between segments
        "selected_color": XYLEM_BLUE,
        "selected_hover_color": UI_HOVER_BLUE,
        "unselected_color": DEPENDABLE_BLUE,
        "unselected_hover_color": UI_DARK_HOVER,
        "text_color": NEUTRAL_WHITE,
        "text_color_disabled": NEUTRAL_MED_GR,
    }


def tabview_colors():
    """The same scheme, under the names CTkTabview gives its tab strip."""
    seg = segmented_button_colors()
    return {
        "segmented_button_fg_color": seg["fg_color"],
        "segmented_button_selected_color": seg["selected_color"],
        "segmented_button_selected_hover_color": seg["selected_hover_color"],
        "segmented_button_unselected_color": seg["unselected_color"],
        "segmented_button_unselected_hover_color": seg["unselected_hover_color"],
        "text_color": seg["text_color"],
    }


# ==============================================================================
# TYPOGRAPHY
# ==============================================================================

FONT_FAMILY_PREFERRED = "Roboto"
FONT_FAMILY_FALLBACK  = "Arial"

_RESOLVED_FAMILY = None


def resolve_font_family():
    """
    Check whether Roboto is installed on this system.
      Yes -> Roboto (Xylem primary typeface)
      No  -> Arial  (Xylem approved desktop system font)

    The result is cached, so probing only ever costs one throwaway Tk root.
    """
    global _RESOLVED_FAMILY
    if _RESOLVED_FAMILY is not None:
        return _RESOLVED_FAMILY

    try:
        probe = tk.Tk()
        probe.withdraw()
        available = set(tkfont.families())
        probe.destroy()
        if FONT_FAMILY_PREFERRED in available:
            print("[Typography] Using Roboto (Xylem primary typeface)")
            _RESOLVED_FAMILY = FONT_FAMILY_PREFERRED
        else:
            print("[Typography] Roboto not installed — using Arial (Xylem desktop system font)")
            _RESOLVED_FAMILY = FONT_FAMILY_FALLBACK
    except Exception:
        _RESOLVED_FAMILY = FONT_FAMILY_FALLBACK

    return _RESOLVED_FAMILY


def get_font(size=12, weight="normal", family=None):
    """
    Build a font tuple in the resolved Xylem typeface.

    Returns a plain tuple so it works with both CustomTkinter widgets and
    classic Tk/ttk widgets.
    """
    return (family or resolve_font_family(), size, weight)


# ==============================================================================
# CLASSIC TTK WIDGET STYLING
# ==============================================================================
# CustomTkinter has no table widget, so both windows fall back to ttk.Treeview.
# Left unstyled those tables look foreign next to the CTk surfaces; this brings
# them into the Xylem palette.

def apply_treeview_style(style_name="Xylem.Treeview", row_height=24):
    """
    Apply Xylem-branded styling to ttk.Treeview widgets.

    Returns the style name to pass as a widget's `style=` argument.
    Safe to call more than once and safe to call before any Treeview exists.
    """
    try:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        base_font = get_font(9)
        head_font = get_font(9, "bold")

        style.configure(
            style_name,
            background=NEUTRAL_WHITE,
            fieldbackground=NEUTRAL_WHITE,
            foreground=NEUTRAL_DARK_GR,
            rowheight=row_height,
            borderwidth=0,
            font=base_font,
        )
        style.configure(
            f"{style_name}.Heading",
            background=DEPENDABLE_BLUE,
            foreground=NEUTRAL_WHITE,
            relief="flat",
            font=head_font,
        )
        style.map(
            f"{style_name}.Heading",
            background=[("active", XYLEM_BLUE)],
        )
        style.map(
            style_name,
            background=[("selected", XYLEM_BLUE)],
            foreground=[("selected", NEUTRAL_WHITE)],
        )
    except Exception as e:
        print(f"[Theme] Treeview styling skipped: {e}")

    return style_name
