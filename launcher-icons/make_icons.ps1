# Render the two launcher icons (Apple Music-style squircle + beamed note + start/stop badge) and pack them as .ico.
param([Parameter(Mandatory = $true)][string]$OutDir, [string]$PreviewDir)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
if ($PreviewDir) { New-Item -ItemType Directory -Force -Path $PreviewDir | Out-Null }

function New-RoundedRect([single]$x, [single]$y, [single]$w, [single]$h, [single]$r) {
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $r * 2
    $path.AddArc($x, $y, $d, $d, 180, 90)
    $path.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $path.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $path.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    return $path
}

function Draw-Icon([int]$size, [string]$kind) {
    $bmp = New-Object System.Drawing.Bitmap $size, $size, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'; $g.InterpolationMode = 'HighQualityBicubic'; $g.PixelOffsetMode = 'HighQuality'
    $g.Clear([System.Drawing.Color]::Transparent)
    $s = $size / 256.0
    $g.ScaleTransform($s, $s)
    if ($kind -eq 'start') { $top = [System.Drawing.Color]::FromArgb(255, 252, 96, 122); $bottom = [System.Drawing.Color]::FromArgb(255, 238, 30, 60) }
    else { $top = [System.Drawing.Color]::FromArgb(255, 108, 108, 118); $bottom = [System.Drawing.Color]::FromArgb(255, 44, 44, 50) }
    # Squircle background with a soft top highlight.
    $tile = New-RoundedRect 8 8 240 240 58
    $bg = New-Object System.Drawing.Drawing2D.LinearGradientBrush ([System.Drawing.PointF]::new(0, 6)), ([System.Drawing.PointF]::new(0, 250)), $top, $bottom
    $g.FillPath($bg, $tile)
    # Span the whole tile and fade out via a colour blend: GDI+ gradients tile outside their range.
    $shine = New-Object System.Drawing.Drawing2D.LinearGradientBrush ([System.Drawing.PointF]::new(0, 7)), ([System.Drawing.PointF]::new(0, 249)), ([System.Drawing.Color]::FromArgb(46, 255, 255, 255)), ([System.Drawing.Color]::FromArgb(0, 255, 255, 255))
    $blend = New-Object System.Drawing.Drawing2D.ColorBlend 3
    $blend.Colors = [System.Drawing.Color[]]@([System.Drawing.Color]::FromArgb(46, 255, 255, 255), [System.Drawing.Color]::FromArgb(0, 255, 255, 255), [System.Drawing.Color]::FromArgb(0, 255, 255, 255))
    $blend.Positions = [single[]]@(0, 0.55, 1)
    $shine.InterpolationColors = $blend
    $g.FillPath($shine, $tile)
    # Beamed eighth notes (the Apple Music glyph shape), white.
    $white = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
    foreach ($head in @(@(80, 164), @(152, 144))) {
        $state = $g.Save()
        $g.TranslateTransform($head[0], $head[1]); $g.RotateTransform(-22)
        $g.FillEllipse($white, -31, -23, 62, 46)
        $g.Restore($state)
    }
    $g.FillRectangle($white, 97, 70, 14, 92)
    $g.FillRectangle($white, 169, 50, 14, 92)
    $beam = New-Object System.Drawing.Drawing2D.GraphicsPath
    $beam.AddPolygon([System.Drawing.PointF[]]@([System.Drawing.PointF]::new(97, 62), [System.Drawing.PointF]::new(183, 40), [System.Drawing.PointF]::new(183, 72), [System.Drawing.PointF]::new(97, 94)))
    $g.FillPath($white, $beam)
    # Badge: a ring in the tile colour separates it from the note.
    $ring = New-Object System.Drawing.SolidBrush $bottom
    $g.FillEllipse($ring, 150, 150, 104, 104)
    $g.FillEllipse($white, 158, 158, 88, 88)
    if ($kind -eq 'start') {
        $red = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 238, 30, 60))
        $g.FillPolygon($red, [System.Drawing.PointF[]]@([System.Drawing.PointF]::new(190, 178), [System.Drawing.PointF]::new(190, 226), [System.Drawing.PointF]::new(228, 202)))
    } else {
        $dark = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 44, 44, 50))
        $g.FillPath($dark, (New-RoundedRect 183 183 38 38 7))
    }
    $g.Dispose()
    return $bmp
}

foreach ($kind in 'start', 'stop') {
    $frames = @()
    foreach ($size in 256, 64, 48, 32, 24, 16) {
        $bmp = Draw-Icon $size $kind
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        if ($PreviewDir -and $size -eq 256) { $bmp.Save((Join-Path $PreviewDir "remote-$kind-256.png"), [System.Drawing.Imaging.ImageFormat]::Png) }
        if ($PreviewDir -and $size -eq 32) { $bmp.Save((Join-Path $PreviewDir "remote-$kind-32.png"), [System.Drawing.Imaging.ImageFormat]::Png) }
        $bmp.Dispose()
        $frames += , @($size, $ms.ToArray())
    }
    # ICO container with PNG-compressed frames (supported since Windows Vista).
    $out = New-Object System.IO.MemoryStream
    $w = New-Object System.IO.BinaryWriter $out
    $w.Write([UInt16]0); $w.Write([UInt16]1); $w.Write([UInt16]$frames.Count)
    $offset = 6 + 16 * $frames.Count
    foreach ($frame in $frames) {
        $dim = if ($frame[0] -ge 256) { 0 } else { $frame[0] }
        $w.Write([Byte]$dim); $w.Write([Byte]$dim); $w.Write([Byte]0); $w.Write([Byte]0)
        $w.Write([UInt16]1); $w.Write([UInt16]32); $w.Write([UInt32]$frame[1].Length); $w.Write([UInt32]$offset)
        $offset += $frame[1].Length
    }
    foreach ($frame in $frames) { $w.Write([byte[]]$frame[1]) }
    $w.Flush()
    [IO.File]::WriteAllBytes((Join-Path $OutDir "remote-$kind.ico"), $out.ToArray())
    Write-Output ("remote-{0}.ico {1} bytes" -f $kind, $out.Length)
}
