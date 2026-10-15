// Standalone: rustc --edition 2024 --test src/rx_packet.rs -o /tmp/rx-packet-check && /tmp/rx-packet-check
pub fn valid(bytes: &[u8], sequence: u16, stream: bool) -> bool {
    bytes.len() == 35
        && bytes[..5] == [5, 0x42, 0x20, 30, 0]
        && bytes[5..7] == sequence.to_le_bytes()
        && bytes[7..9] == [26, 0]
        && bytes[9..11]
            == if stream {
                sequence.to_le_bytes()
            } else {
                [0, 1]
            }
        && bytes[11..].iter().copied().eq(2..26)
}

#[cfg(test)]
mod tests {
    use super::valid;

    #[test]
    fn rejects_wrong_envelope_status_sequence_length_or_fixture() {
        let mut packet = vec![5, 0x42, 0x20, 30, 0, 0, 0, 26, 0];
        packet.extend(0..26);
        assert!(valid(&packet, 0, false));
        for index in 0..packet.len() {
            packet[index] ^= 1;
            assert!(!valid(&packet, 0, false), "byte {index}");
            packet[index] ^= 1;
        }
        for length in 0..35 {
            assert!(!valid(&packet[..length], 0, false));
        }
        packet.push(0);
        assert!(!valid(&packet, 0, false));
    }

    #[test]
    fn stream_requires_matching_hci_and_radio_sequences() {
        let mut packet = vec![5, 0x42, 0x20, 30, 0, 0, 0, 26, 0];
        packet.extend(0..26);
        for sequence in 0u16..500 {
            packet[5..7].copy_from_slice(&sequence.to_le_bytes());
            packet[9..11].copy_from_slice(&sequence.to_le_bytes());
            assert!(valid(&packet, sequence, true));
            assert!(!valid(&packet, sequence.wrapping_add(1), true));
            packet[9] ^= 1;
            assert!(!valid(&packet, sequence, true));
        }
    }
}
