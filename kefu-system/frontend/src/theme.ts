import { createTheme } from '@mui/material/styles'

/** MUI 主题：紧凑桌面密度，贴合"本地控制台"形态。 */
export const theme = createTheme({
  palette: {
    mode: 'light',
    primary: { main: '#07c160' }, // 微信绿
    secondary: { main: '#576b95' }, // 微信链接蓝
    background: { default: '#f5f6f7', paper: '#ffffff' },
  },
  typography: {
    fontFamily:
      '"Microsoft YaHei", "PingFang SC", "Helvetica Neue", Arial, sans-serif',
    fontSize: 13,
  },
  components: {
    MuiButton: { defaultProps: { size: 'small' } },
    MuiTextField: { defaultProps: { size: 'small' } },
    MuiFormControl: { defaultProps: { size: 'small' } },
    MuiTableCell: { styleOverrides: { root: { padding: '6px 10px' } } },
  },
})
