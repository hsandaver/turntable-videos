"""Verify that orientation corrections happen before the turntable rotation."""
import unittest
from unittest.mock import patch

import numpy as np

import render


class OrientationTests(unittest.TestCase):
    def test_sideways_and_flat_objects_stay_upright_for_whole_turn(self):
        for angles, long_axis in (((0, 0, 90), (1, 0, 0, 0)),
                                  ((-90, 0, 0), (0, 0, 1, 0))):
            with self.subTest(angles=angles), patch.object(render, "create_context") as create:
                ctx = create.return_value
                ctx.max_samples = 4
                ctx.framebuffer.return_value.read.return_value = bytes(16 * 9 * 3)
                renderer = render.Renderer(width=16, height=9)
                renderer.set_orientation(angles)
                renderer.view_proj = np.identity(4)
                for fraction in (0, 0.25, 0.5, 0.75):
                    renderer.frame(fraction * render.FRAMES)
                    payload = renderer.program["mvp"].write.call_args.args[0]
                    transform = np.frombuffer(payload, dtype="f4").reshape(4, 4).T
                    np.testing.assert_allclose(transform @ long_axis, (0, 1, 0, 0), atol=1e-6)
                renderer.release()

    def test_tilted_corners_fit_throughout_turn(self):
        with patch.object(render, "create_context") as create:
            create.return_value.max_samples = 4
            renderer = render.Renderer(width=640, height=360)
            renderer.bounds = np.array(list(render.product((-1, 1), repeat=3)))
            renderer.set_orientation((35, 20, 45))
            corners = np.column_stack((renderer.bounds, np.ones(8)))
            for angle in np.linspace(0, 2 * np.pi, 72):
                transform = renderer.view_proj @ render.rotation_y(angle) @ renderer.orientation
                projected = corners @ transform.T
                self.assertLessEqual(np.max(np.abs(projected[:, :2])), 0.951)
            renderer.release()


if __name__ == "__main__":
    unittest.main()
