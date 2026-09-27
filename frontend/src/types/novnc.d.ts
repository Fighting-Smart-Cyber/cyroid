// @novnc/novnc ships no typings. Only the surface KubeVirtConsole uses is declared.
declare module '@novnc/novnc/lib/rfb' {
  export interface RFBCredentials {
    username?: string
    password?: string
    target?: string
  }

  export interface RFBOptions {
    shared?: boolean
    credentials?: RFBCredentials
    wsProtocols?: string[]
  }

  export interface RFBClipboardEvent extends CustomEvent<{ text: string }> {}
  export interface RFBDisconnectEvent extends CustomEvent<{ clean: boolean }> {}
  export interface RFBCredentialsEvent extends CustomEvent<{ types: string[] }> {}

  export default class RFB {
    constructor(target: HTMLElement, urlOrChannel: string | WebSocket, options?: RFBOptions)
    viewOnly: boolean
    focusOnClick: boolean
    clipViewport: boolean
    dragViewport: boolean
    scaleViewport: boolean
    resizeSession: boolean
    showDotCursor: boolean
    background: string
    qualityLevel: number
    compressionLevel: number
    disconnect(): void
    sendCredentials(credentials: RFBCredentials): void
    sendKey(keysym: number, code: string | null, down?: boolean): void
    sendCtrlAltDel(): void
    focus(): void
    blur(): void
    clipboardPasteFrom(text: string): void
    addEventListener(type: 'connect', listener: (e: Event) => void): void
    addEventListener(type: 'disconnect', listener: (e: RFBDisconnectEvent) => void): void
    addEventListener(type: 'clipboard', listener: (e: RFBClipboardEvent) => void): void
    addEventListener(type: 'credentialsrequired', listener: (e: RFBCredentialsEvent) => void): void
    addEventListener(type: 'securityfailure', listener: (e: CustomEvent<{ status: number; reason?: string }>) => void): void
    removeEventListener(type: string, listener: (e: never) => void): void
  }
}
