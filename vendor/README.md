# Bundled install files (offline install)

These are the unmodified Python "wheel" files from PyPI (https://pypi.org) for the one
dependency the owner approved, Plotly, and the two packages it needs. They let the
reports be installed without internet access:

    .venv\Scripts\python -m pip install --no-index --find-links vendor\wheels -r requirements.txt

| File | Licence | SHA-256 |
|---|---|---|
| plotly-7.1.0-py3-none-any.whl | MIT | dbb7fa18afce40d0a8e80d1bf162eceb3faa0ce5a77fe741ad09a74cf78f53f3 |
| narwhals-2.27.1-py3-none-any.whl | MIT | d057df13f5852b8e157596e82eb5e955fad267425df5e420e0ee9863da483b31 |
| packaging-26.3-py3-none-any.whl | Apache-2.0 or BSD-2-Clause | d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c |

All three are pure Python, so the same files work on Windows, macOS and Linux.
Only the reports use them; everything else in the toolkit needs the standard library only.
