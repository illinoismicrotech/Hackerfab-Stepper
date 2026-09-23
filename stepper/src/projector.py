from PIL import Image

_DLP_W, _DLP_H = 1280, 720


class ProjectorController:
    def show(self, image: Image.Image):
        pass

    def size(self) -> tuple[int, int]:
        return (_DLP_W, _DLP_H)

    def clear(self):
        pass


class TkProjector(ProjectorController):
    """Projector image state, displayed by the app's on-demand fullscreen view.

    Canvas dimensions stay fixed so opening/closing a preview cannot change
    pattern processing or alignment coordinates. No second window is created.
    """
    def __init__(self, root, title='Projector', background='#000000'):
        self.current_image = Image.new('RGB', self.size(), 'black')
        self.on_show = None

    def show(self, image: Image.Image):
        self.current_image = image
        if self.on_show:
            self.on_show()

    def clear(self):
        self.show(Image.new('RGB', self.size(), 'black'))
