# Copy this file to network.local.psd1 and put in your own home network.
# network.local.psd1 is ignored by Git, so your Wi-Fi name and address stay on your PC.
# Start-Remote.ps1 only starts on this private Wi-Fi with this address, and
# Configure-Network.ps1 (run as administrator) only lets this subnet reach port 8765.
# Look the values up with:  Get-NetConnectionProfile   and   Get-NetIPAddress -AddressFamily IPv4
@{
    InterfaceIndex = 0                  # InterfaceIndex of your Wi-Fi adapter
    WifiName       = 'Your Wi-Fi name'  # Name shown by Get-NetConnectionProfile
    Address        = '192.168.1.50'     # this PC's IPv4 address (reserve it in your router)
    PrefixLength   = 24
    Subnet         = '192.168.1.0/24'   # the LAN range allowed to connect
}
