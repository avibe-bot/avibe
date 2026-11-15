# Windows PowerShell 5.1 provides the OS UI Automation assemblies. This controls
# only the real Avibe tray/menu in this disposable user's desktop; no screenshot.
$ErrorActionPreference = 'Stop'
$phase = 'load_uia'
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $scope = [System.Windows.Automation.TreeScope]::Descendants
    $button = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
    $phase = 'find_taskbar'
    $tray = $root.FindFirst($scope, [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ClassNameProperty, 'Shell_TrayWnd'))
    if (-not $tray) { throw 'interactive_taskbar_not_found' }
    $icons = @($tray.FindAll($scope, $button) | Where-Object { $_.Current.Name -like 'Avibe*' })
    if ($icons.Count -eq 0) {
        $phase = 'open_overflow'
        $expand = @($tray.FindAll($scope, $button) | Where-Object { $_.Current.Name -eq 'Show hidden icons' })
        if ($expand.Count -ne 1) { throw 'tray_overflow_control_unavailable' }
        $expand[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        $deadline = (Get-Date).AddSeconds(5)
        do {
            $overflow = $root.FindFirst($scope, [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ClassNameProperty, 'NotifyIconOverflowWindow'))
            if ($overflow) { $icons = @($overflow.FindAll($scope, $button) | Where-Object { $_.Current.Name -like 'Avibe*' }) }
            if ($icons.Count -eq 1) { break }
            Start-Sleep -Milliseconds 100
        } while ((Get-Date) -lt $deadline)
    }
    if ($icons.Count -ne 1) { throw 'avibe_tray_icon_not_unique' }
    $phase = 'invoke_tray_icon'
    $icons[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    $phase = 'find_off_menu_item'
    $menuCondition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::MenuItem)
    $deadline = (Get-Date).AddSeconds(5)
    do {
        $items = @($root.FindAll($scope, $menuCondition) | Where-Object { $_.Current.Name -like 'Computer Use*' -and -not $_.Current.IsOffscreen })
        if ($items.Count -eq 1) { break }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    if ($items.Count -ne 1 -or -not $items[0].Current.IsEnabled) { throw 'computer_use_tray_item_unavailable' }
    # The caller separately proved ready immediately before this user action
    # and will require status=off plus driver/helper exit afterwards.
    $phase = 'invoke_off_menu_item'
    $items[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    '{"ok":true}'
} catch {
    @{ok=$false; reason='native_tray_off_unproven'; phase=$phase; error_class=$_.Exception.GetBaseException().GetType().FullName} | ConvertTo-Json -Compress
    exit 1
}
