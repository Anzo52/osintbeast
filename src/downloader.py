import requests
import zipfile
import io
import os

def download_github_repo(repo_url, output_dir):
    """
    Download a GitHub repository as a zip file and extract it to a specified directory.

    :param repo_url: URL of the GitHub repository.
    :param output_dir: Directory where the repository will be extracted.
    """
    if not repo_url.endswith('/'):
        repo_url += '/'

    zip_url = f"{repo_url}archive/main.zip"

    try:
        response = requests.get(zip_url)
        response.raise_for_status()  # Raise an exception for HTTP errors

        # Extract the zip file
        with zipfile.ZipFile(io.BytesIO(response.content)) as thezip:
            thezip.extractall(output_dir)
        print(f"Repository downloaded and extracted to {output_dir}")

    except requests.RequestException as e:
        print(f"Error downloading repository: {e}")

def download_tool(tool_name):
    """
    Download an OSINT tool by its name. This function can be expanded to map tool names to their GitHub URLs.

    :param tool_name: Name of the tool to download.
    """
    # Example mapping, this should be expanded based on your tool requirements
    tool_url_mapping = {
        "example_tool": "https://github.com/user/example_tool"
    }

    if tool_name in tool_url_mapping:
        repo_url = tool_url_mapping[tool_name]
        download_github_repo(repo_url, "path/to/download/directory")
    else:
        print(f"Tool {tool_name} not found.")
