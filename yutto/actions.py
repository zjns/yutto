import argparse


class OnlySubtitleAction(argparse.Action):
    def __init__(self, option_strings, dest, help=None):
        super().__init__(option_strings, dest, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        namespace.require_video = False
        namespace.require_audio = False
