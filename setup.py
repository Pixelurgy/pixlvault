from setuptools import setup, find_packages

setup(
    name="pixlvault",
    version="0.7.0",
    description="PixlVault Server",
    author="PixlVault",
    packages=find_packages(),
    install_requires=["fastapi", "uvicorn"],
    python_requires=">=3.7",
)
