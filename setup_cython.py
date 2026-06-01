"""
Setup script to compile Cython modules.

Run: python setup_cython.py build_ext --inplace
"""

from setuptools import setup
from Cython.Build import cythonize
import numpy as np

setup(
    ext_modules=cythonize(
        "mccfr_core.pyx",
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
        },
    ),
    include_dirs=[np.get_include()],
)
