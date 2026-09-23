"""Readable desktop typography and restrained light/dark palettes."""
import tkinter.font as tkfont
import ttkbootstrap as ttk
from ttkbootstrap.style import ThemeDefinition


PALETTES = {
    'studio': ('light', {
        'primary': '#31675b', 'secondary': '#63706d', 'success': '#31675b',
        'info': '#326c9a', 'warning': '#a56e22', 'danger': '#b44747',
        'light': '#f3f5f3', 'dark': '#26332f', 'bg': '#f6f7f5',
        'fg': '#25312d', 'selectbg': '#31675b', 'selectfg': '#ffffff',
        'border': '#d9dfdb', 'inputfg': '#25312d', 'inputbg': '#ffffff',
        'active': '#e6ece7'}),
    'studio-dark': ('dark', {
        'primary': '#85b7a4', 'secondary': '#a9b5af', 'success': '#85b7a4',
        'info': '#a0bdd8', 'warning': '#dfbb82', 'danger': '#e09b99',
        'light': '#e3e9e5', 'dark': '#1d2420', 'bg': '#232b27',
        'fg': '#e3e9e5', 'selectbg': '#3f5147', 'selectfg': '#ffffff',
        'border': '#45514a', 'inputfg': '#e3e9e5', 'inputbg': '#2d3731',
        'active': '#39483f'})}


def configure_theme(root, name='studio-dark', text_scale=1.0):
    style = ttk.Style()
    for key, (kind, colors) in PALETTES.items():
        if key not in style.theme_names():
            style.register_theme(ThemeDefinition(name=key, themetype=kind, colors=colors))
    style.theme_use(name if name in style.theme_names() else 'studio-dark')
    available = set(tkfont.families(root))
    family = next((name for name in ('Inter', 'Segoe UI', 'SF Pro Text', 'Noto Sans', 'Liberation Sans', 'Arial') if name in available), 'sans')
    text_scale = max(1.0, min(1.5, float(text_scale)))
    for font_name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont', 'TkTooltipFont'):
        tkfont.nametofont(font_name, root=root).configure(family=family, size=round(12 * text_scale))
    # Named fonts also update existing headings when the user changes text size.
    for name, size, weight in (('StepperTitle', 24, 'bold'), ('StepperBrand', 18, 'bold'), ('StepperSection', 14, 'bold')):
        if name in tkfont.names(root):
            font = tkfont.nametofont(name, root=root)
        else:
            font = tkfont.Font(root=root, name=name, exists=False)
            if not hasattr(root, '_stepper_fonts'):
                root._stepper_fonts = []
            root._stepper_fonts.append(font)
        font.configure(family=family, size=round(size * text_scale), weight=weight)
    style.configure('.', font='TkDefaultFont')
    style.configure('TButton', padding=(16, 10))
    style.configure('TEntry', padding=(12, 10))
    style.configure('TCombobox', padding=(12, 10))
    style.configure('TNotebook.Tab', padding=(18, 12))
    style.configure('TLabelframe', padding=16)
    style.configure('TLabelframe.Label', font='StepperSection')
    style.configure('Treeview', rowheight=round(36 * text_scale))
    root.option_add('*TCombobox*Listbox.font', 'TkDefaultFont')
    root.option_add('*TCombobox*Listbox.borderWidth', 12)
    root.option_add('*TCombobox*Listbox.relief', 'flat')
    root.option_add('*TCombobox*Listbox.background', style.colors.inputbg)
    root.option_add('*TCombobox*Listbox.foreground', style.colors.fg)
    return style


def prepare_dropdowns(root):
    """Inset popup contents so compositor-rounded corners cannot clip the text."""
    def visit(widget):
        if isinstance(widget, ttk.Combobox):
            def prepare(w=widget):
                popup = w.tk.call('ttk::combobox::PopdownWindow', str(w))
                colors = ttk.Style().colors
                w.tk.call(f'{popup}.f.l', 'configure', '-font', 'TkDefaultFont',
                          '-borderwidth', 12, '-relief', 'flat', '-highlightthickness', 0,
                          '-background', colors.inputbg, '-foreground', colors.fg,
                          '-selectbackground', colors.selectbg, '-selectforeground', colors.selectfg)
            widget.configure(postcommand=prepare, height=8)
        for child in widget.winfo_children():
            visit(child)
    visit(root)
