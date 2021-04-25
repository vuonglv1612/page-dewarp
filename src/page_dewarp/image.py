import cv2
import numpy as np
from pathlib import Path
from scipy.optimize import minimize
from .debug_utils import debug_show
from .mask import Mask
from .options import cfg
from .optimise import optimise_params
from .projection import project_xy
from .dewarp import RemappedImage
from .spans import assemble_spans, sample_spans, keypoints_from_samples
from .solve import get_default_params


def imgsize(img):
    height, width = img.shape[:2]
    return "{}x{}".format(width, height)


def get_page_dims(corners, rough_dims, params):
    dst_br = corners[2].flatten()
    dims = np.array(rough_dims)

    def objective(dims):
        proj_br = project_xy(dims, params)
        return np.sum((dst_br - proj_br.flatten()) ** 2)

    res = minimize(objective, dims, method="Powell")
    dims = res.x
    print("  got page dims", dims[0], "x", dims[1])
    return dims


class WarpedImage:
    written = False  # Explicitly declare the file write in this attribute

    def __init__(self, imgfile):
        self.cv2_img = cv2.imread(imgfile)
        self.file_path = Path(imgfile)
        self.small = self.resize_to_screen()
        size, resized = self.size, self.resized
        print(f"Loaded {self.basename} at {size=} --> {resized=}")
        if cfg.debug_lvl_opt.DEBUG_LEVEL >= 3:
            debug_show(self.stem, 0.0, "original", self.small)

        self.calculate_page_extents()  # set pagemask & page_outline attributes
        self.contour_list = self.contour_info(text=True)
        spans = self.iteratively_assemble_spans()
        # Skip if no spans
        if len(spans) < 1:
            print(f"skipping {self.stem} because only {len(spans)} spans")
        else:
            span_points = sample_spans(self.small.shape, spans)
            n_pts = sum(map(len, span_points))
            print(f"  got {len(spans)} spans with {n_pts} points.")

            corners, ycoords, xcoords = keypoints_from_samples(
                self.stem, self.small, self.pagemask, self.page_outline, span_points
            )
            rough_dims, span_counts, params = get_default_params(
                corners, ycoords, xcoords
            )
            dstpoints = np.vstack((corners[0].reshape((1, 1, 2)),) + tuple(span_points))
            params = optimise_params(
                self.stem,
                self.small,
                dstpoints,
                span_counts,
                params,
                cfg.debug_lvl_opt.DEBUG_LEVEL,
            )
            page_dims = get_page_dims(corners, rough_dims, params)
            self.threshold(page_dims, params)
            self.written = True

    def threshold(self, page_dims, params):
        remap = RemappedImage(self.stem, self.cv2_img, self.small, page_dims, params)
        self.outfile = remap.threshfile

    def iteratively_assemble_spans(self):
        """
        First try to assemble spans from contours, if too few spans then make spans by
        line detection (borders of a table box) rather than text detection.
        """
        spans = assemble_spans(self.stem, self.small, self.pagemask, self.contour_list)
        # Retry if insufficient spans
        if len(spans) < 3:
            print(f"  detecting lines because only {len(spans)} text spans")
            self.contour_list = self.contour_info(line=True)
            spans = self.attempt_reassemble_spans(spans)
        return spans

    def attempt_reassemble_spans(self, prev_spans):
        new_spans = assemble_spans(
            self.stem, self.small, self.pagemask, self.contour_list
        )
        return new_spans if len(new_spans) > len(prev_spans) else prev_spans

    @property
    def basename(self):
        return self.file_path.name

    @property
    def stem(self):
        return self.file_path.stem

    def resize_to_screen(self, maxw=1280, maxh=700, copy=False):
        height, width = self.cv2_img.shape[:2]
        scl_x = float(width) / maxw
        scl_y = float(height) / maxh
        scl = int(np.ceil(max(scl_x, scl_y)))
        if scl > 1.0:
            inv_scl = 1.0 / scl
            img = cv2.resize(
                self.cv2_img, (0, 0), None, inv_scl, inv_scl, cv2.INTER_AREA
            )
        elif copy:
            img = self.cv2_img.copy()
        else:
            img = self.cv2_img
        return img

    def calculate_page_extents(self):
        height, width = self.small.shape[:2]
        # xmin, ymin = map(cfg.image_opts, map("PAGE_MARGIN_".__add__, "XY"))
        xmin = cfg.image_opts.PAGE_MARGIN_X
        ymin = cfg.image_opts.PAGE_MARGIN_Y
        xmax, ymax = (width - xmin), (height - ymin)
        self.pagemask = np.zeros((height, width), dtype=np.uint8)
        cv2.rectangle(self.pagemask, (xmin, ymin), (xmax, ymax), (255, 255, 255), -1)
        self.page_outline = np.array(
            [[xmin, ymin], [xmin, ymax], [xmax, ymax], [xmax, ymin]]
        )

    @property
    def size(self):
        return imgsize(self.cv2_img)

    @property
    def resized(self):
        return imgsize(self.small)

    def contour_info(self, text=False, line=False):
        if not (text ^ line):
            raise ValueError("Please specify either text or line contour")
        c_type = "text" if text else "line"
        mask = Mask(self.stem, self.small, self.pagemask, c_type)
        return mask.contours()