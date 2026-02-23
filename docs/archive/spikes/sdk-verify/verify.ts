import { 
  EvenAppBridge, 
  waitForEvenAppBridge,
  TextContainerProperty,
  CreateStartUpPageContainer,
  StartUpPageCreateResult,
  EvenAppMethod,
  OsEventTypeList,
} from '@evenrealities/even_hub_sdk';
import type { AudioEventPayload, EvenHubEvent } from '@evenrealities/even_hub_sdk';

// P0.1: Check if setPageFlip exists on the bridge
async function testPageFlip() {
  const bridge = await waitForEvenAppBridge();
  
  // Does this compile? If not, what's the error?
  // @ts-expect-error — testing if method exists
  await bridge.setPageFlip(1);
  
  // Check all available methods
  type BridgeMethods = keyof EvenAppBridge;
  type HasSetPageFlip = 'setPageFlip' extends BridgeMethods ? true : false;
  const hasIt: HasSetPageFlip = false; // Will error if it's actually true
}

// P0.3: Check content field name
function testContentField() {
  const container = new TextContainerProperty({
    containerID: 1,
    containerName: 'test',
    content: 'Hello',
    xPosition: 0,
    yPosition: 0,
    width: 480,
    height: 100,
  });
  
  // Does 'content' exist as a property?
  const c: string | undefined = container.content;
  
  // Does 'textContent' exist?
  // @ts-expect-error — testing if textContent exists
  const tc: string = container.textContent;
}

// P0.4: Check audioPcm type
function testAudioType(event: EvenHubEvent) {
  if (event.audioEvent) {
    const pcm = event.audioEvent.audioPcm;
    // What type does TypeScript infer for pcm?
    const isUint8: Uint8Array = pcm; // Will error if it's Int8Array
  }
}

// P0.5: Check StartUpPageCreateResult
function testCreateResult() {
  const success = StartUpPageCreateResult.success; // Should be 0
  const invalid = StartUpPageCreateResult.invalid; // Should be 1
  const oversize = StartUpPageCreateResult.oversize; // Should be 2  
  const oom = StartUpPageCreateResult.outOfMemory; // Should be 3
  
  // Verify actual numeric values
  const s: 0 = StartUpPageCreateResult.success;
  const i: 1 = StartUpPageCreateResult.invalid;
  const o: 2 = StartUpPageCreateResult.oversize;
  const m: 3 = StartUpPageCreateResult.outOfMemory;
}
