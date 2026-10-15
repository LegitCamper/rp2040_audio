// Standalone: rustc --edition 2024 --test src/rx_packet.rs -o /tmp/rx-packet-check && /tmp/rx-packet-check
/// `sequence` is the controller's HCI packet sequence number, which counts
/// accepted SDUs. `payload` is the transmitter's event index carried in the
/// marker. They coincide only when nothing was lost, so they are checked
/// separately. Fixed-channel callers pass the constant fixture value 0x0100.
pub fn valid(bytes: &[u8], sequence: u16, payload: u16) -> bool {
    bytes.len() == 35
        && bytes[..5] == [5, 0x42, 0x20, 30, 0]
        && bytes[5..7] == sequence.to_le_bytes()
        && bytes[7..9] == [26, 0]
        && bytes[9..11] == payload.to_le_bytes()
        && bytes[11..].iter().copied().eq(2..26)
}

#[cfg(test)]
mod tests {
    use super::valid;

    #[test]
    fn rejects_wrong_envelope_status_sequence_length_or_fixture() {
        let mut packet = vec![5, 0x42, 0x20, 30, 0, 0, 0, 26, 0];
        packet.extend(0..26);
        assert!(valid(&packet, 0, 0x0100));
        for index in 0..packet.len() {
            packet[index] ^= 1;
            assert!(!valid(&packet, 0, 0x0100), "byte {index}");
            packet[index] ^= 1;
        }
        for length in 0..35 {
            assert!(!valid(&packet[..length], 0, 0x0100));
        }
        packet.push(0);
        assert!(!valid(&packet, 0, 0x0100));
    }

    #[test]
    fn hci_sequence_and_payload_index_are_checked_independently() {
        let mut packet = vec![5, 0x42, 0x20, 30, 0, 0, 0, 26, 0];
        packet.extend(0..26);
        for sequence in 0u16..500 {
            packet[5..7].copy_from_slice(&sequence.to_le_bytes());
            packet[9..11].copy_from_slice(&sequence.to_le_bytes());
            assert!(valid(&packet, sequence, sequence));
            assert!(!valid(&packet, sequence.wrapping_add(1), sequence));
            assert!(!valid(&packet, sequence, sequence.wrapping_add(1)));
        }
        // Loss makes the two fields diverge, which must still be accepted.
        packet[5..7].copy_from_slice(&30u16.to_le_bytes());
        packet[9..11].copy_from_slice(&42u16.to_le_bytes());
        assert!(valid(&packet, 30, 42));
        assert!(!valid(&packet, 42, 42));
        assert!(!valid(&packet, 30, 30));
    }
}
