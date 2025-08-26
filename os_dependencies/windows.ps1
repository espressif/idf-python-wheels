# Windows MSYS2 and GTK4 setup script for GitHub Actions
Write-Host "Setting up MSYS2 and GTK4 dependencies..."

# MSYS2 installation paths and URL
$msys2InstallPath = "C:\msys64"
$msys2Installer = "msys2-x86_64-latest.exe"
$installerUrl = "https://github.com/msys2/msys2-installer/releases/download/nightly-x86_64/msys2-x86_64-latest.exe"

# Install MSYS2 if not already present
if (-not (Test-Path $msys2InstallPath)) {
    Write-Host "Downloading and installing MSYS2..."
    Invoke-WebRequest -Uri $installerUrl -OutFile $msys2Installer -UseBasicParsing
    Start-Process -FilePath $msys2Installer -ArgumentList "install", "--confirm-command", "--accept-messages", "--root", $msys2InstallPath -Wait -NoNewWindow
    Remove-Item $msys2Installer -ErrorAction SilentlyContinue
}

# Define MSYS2 bash executable
$msys2Exe = "$msys2InstallPath\usr\bin\bash.exe"

# Update packages and install PyGObject dependencies
Write-Host "Updating MSYS2 and installing GTK4 dependencies..."
& $msys2Exe -lc "pacman-key --init"
& $msys2Exe -lc "pacman-key --populate msys2"
& $msys2Exe -lc "pacman -Suy --noconfirm"
& $msys2Exe -lc "pacman -S --noconfirm mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-python3 mingw-w64-ucrt-x86_64-python3-gobject"

# Add MSYS2 to PATH
$env:PATH = "$msys2InstallPath\ucrt64\bin;$env:PATH"

Write-Host "MSYS2 and GTK4 setup completed!"
